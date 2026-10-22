#!/usr/bin/env bash
# openviking 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + AGFS memfs 挂载/写入/回读 smoke + 文件系统操作(stat/ls/mkdir/grep/rm)smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 基础 smoke:挂载 memfs,写入并回读固定负载,校验逐字节一致。
write_agfs_readwrite_smoke() {
    cat >"$1" <<'PY'
from openviking.pyagfs import get_binding_client

BindingClient, _FileHandle = get_binding_client()
client = BindingClient()
mount_path = "viking://func-rw/"
client.mount("memfs", mount_path)
try:
    payload = b"openviking-functional-smoke-payload"
    client.write(f"{mount_path}smoke.bin", payload)
    data = client.read(f"{mount_path}smoke.bin")
    if not isinstance(data, (bytes, bytearray)):
        raise SystemExit(f"unexpected read type: {type(data)!r}")
    assert bytes(data) == payload, "read-back payload differs from the written data"
    print("openviking AGFS write/read roundtrip ok")
finally:
    try:
        client.rm(mount_path, recursive=True)
    except Exception:
        pass
    client.unmount(mount_path)
PY
}

# 更完整的功能:stat/ls/mkdir/grep/rm 一整套文件系统操作。
write_agfs_operations_smoke() {
    cat >"$1" <<'PY'
from openviking.pyagfs import get_binding_client


def names_of(entries):
    result = []
    for entry in entries:
        if isinstance(entry, (bytes, bytearray)):
            result.append(bytes(entry).decode("utf-8", "replace"))
        else:
            result.append(str(entry))
    return result


BindingClient, _FileHandle = get_binding_client()
client = BindingClient()
mount_path = "viking://func-ops/"
client.mount("memfs", mount_path)
try:
    file_path = f"{mount_path}data.txt"
    payload = b"openviking functional grep target\n" * 4
    client.write(file_path, payload)

    client.stat(file_path)

    listing = names_of(client.ls(mount_path))
    assert any("data.txt" in name for name in listing), f"ls missing data.txt: {listing}"

    matches = client.grep(file_path, "grep target", False, True)
    assert matches, "grep found no match for the known pattern"

    subdir = f"{mount_path}subdir/"
    client.mkdir(subdir, "755")
    listing = names_of(client.ls(mount_path))
    assert any("subdir" in name for name in listing), f"ls missing subdir: {listing}"

    client.rm(file_path)
    client.rm(subdir, recursive=True)
    listing = names_of(client.ls(mount_path))
    assert not any("data.txt" in name for name in listing), f"rm left data.txt behind: {listing}"
    print("openviking AGFS stat/ls/mkdir/grep/rm ok")
finally:
    try:
        client.rm(mount_path, recursive=True)
    except Exception:
        pass
    client.unmount(mount_path)
PY
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" r_status="$4" r_detail="$5" o_status="$6" o_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_R_STATUS="${r_status}" FUNC_R_DETAIL="${r_detail}" \
        FUNC_O_STATUS="${o_status}" FUNC_O_DETAIL="${o_detail}" \
        python3 - "${RESULTS_DIR}/functional_result.json" <<'PY'
import json, os, sys

out = sys.argv[1]
env = os.environ.get
payload = {
    "software": env("SOFTWARE_NAME"),
    "category": env("CATEGORY"),
    "version": env("SOFTWARE_VERSION"),
    "os": env("FUNC_OS"),
    "mode": env("FUNC_MODE"),
    "hardware": env("FUNC_HARDWARE") or None,
    "architecture": env("EXPECTED_ARCH"),
    "run_id": env("PERF_RUN_ID"),
    "status": env("FUNC_OVERALL"),
    "checks": [
        {"name": "version", "status": env("FUNC_V_STATUS"), "detail": env("FUNC_V_DETAIL")},
        {"name": "agfs-readwrite", "status": env("FUNC_R_STATUS"), "detail": env("FUNC_R_DETAIL")},
        {"name": "agfs-operations", "status": env("FUNC_O_STATUS"), "detail": env("FUNC_O_DETAIL")},
    ],
}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, out)
PY
}

func_check() {
    initialize_runtime || return $?
    [[ -n "${PYBIN}" && -x "${PYBIN}" ]] || {
        log "ERROR: python interpreter is unavailable"
        return 40
    }
    local log_file="${RESULTS_DIR}/functional_test.log"
    : >"${log_file}"

    local version_status="failed" version_detail="" actual
    local rw_status="failed" rw_detail=""
    local ops_status="failed" ops_detail=""

    # 1) 版本
    actual="$(read_installed_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="openviking ${actual}"
    else
        version_detail="openviking ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi
    printf 'version: %s (%s)\n' "${version_status}" "${version_detail}" >>"${log_file}"

    # 2) 基础 smoke:AGFS 写入/回读
    if write_agfs_readwrite_smoke "${TMPDIR}/openviking_rw.py" && \
        "${PYBIN}" "${TMPDIR}/openviking_rw.py" >>"${log_file}" 2>&1; then
        rw_status="passed"
        rw_detail="memfs mount/write/read roundtrip matched byte-for-byte"
    else
        rw_detail="AGFS write/read smoke failed; see functional_test.log"
    fi
    printf 'agfs-readwrite: %s (%s)\n' "${rw_status}" "${rw_detail}" >>"${log_file}"

    # 3) 文件系统操作矩阵
    if write_agfs_operations_smoke "${TMPDIR}/openviking_ops.py" && \
        "${PYBIN}" "${TMPDIR}/openviking_ops.py" >>"${log_file}" 2>&1; then
        ops_status="passed"
        ops_detail="stat/ls/mkdir/grep/rm sequence behaved as expected"
    else
        ops_detail="AGFS operations smoke failed; see functional_test.log"
    fi
    printf 'agfs-operations: %s (%s)\n' "${ops_status}" "${ops_detail}" >>"${log_file}"

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${rw_status}" != "passed" || "${ops_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${rw_status}" "${rw_detail}" \
        "${ops_status}" "${ops_detail}"
    log "checks: version=${version_status} agfs-readwrite=${rw_status} agfs-operations=${ops_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        log "prebuilt mode: nothing installed by this case to clean"
    else
        [[ -d "${VENV_DIR}" ]] && rm -rf -- "${VENV_DIR}"
        [[ -d "${BUILD_DIR}" ]] && rm -rf -- "${BUILD_DIR}"
        log "removed the task-private venv and build directory"
    fi
    [[ -d "${TMPDIR}" ]] && rm -rf -- "${TMPDIR}/openviking_rw.py" "${TMPDIR}/openviking_ops.py"
    log "cleaned openviking task data"
}

# ---------- 独立运行入口 ----------

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    [[ "${STANDALONE_OWNS_WORK_DIR}" -eq 1 ]] || return 0
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-func/${SOFTWARE_NAME}/local-* ]] || {
        log "ERROR: refusing to remove unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    [[ -d "${PERF_WORK_DIR}" ]] && rm -rf -- "${PERF_WORK_DIR}"
    log "removed standalone work directory: ${PERF_WORK_DIR}"
}

emergency_cleanup() {
    set +e
    func_cleanup
    cleanup_standalone_workdir
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Run the openviking functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    OpenViking version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --mode MODE          prebuilt | build (default: ${FUNC_MODE})
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_test_standalone() {
    local stage_status=0 failed_stage="" finalize_status=0

    configure_runtime_paths || return $?
    trap emergency_cleanup EXIT
    initialize_runtime || return $?

    func_check || {
        stage_status=$?
        failed_stage="check"
    }
    func_cleanup || true
    cleanup_standalone_workdir || finalize_status=70
    trap - EXIT

    log "standalone results: ${RESULTS_DIR}"
    if [[ "${stage_status}" -ne 0 ]]; then
        log "standalone failed at stage: ${failed_stage}"
        return "${stage_status}"
    fi
    return "${finalize_status}"
}

main() {
    parse_common_args "$@"
    case "$?" in
        0) ;;
        64)
            usage
            return 0
            ;;
        *) return 10 ;;
    esac
    mkdir -p "${RESULTS_DIR:-${SCRIPT_DIR}/results}" || return $?
    run_test_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi