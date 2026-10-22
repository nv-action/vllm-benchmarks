#!/usr/bin/env bash
# Zstd 测试脚本:功能判定 + 收尾。
# 判定优先复用上游 tests/playTests.sh 短测套件,叠加版本校验与 CLI 压缩/解压回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/zstd_test.log"
}

# CLI 压缩/解压回环:生成确定性数据 -> 压缩 -> 解压 -> 按 sha256 校验内容一致。
run_cli_roundtrip() {
    local data="${SERVICE_DIR}/roundtrip.bin"
    local packed="${data}.zst"
    local unpacked="${data}.out"
    mkdir -p "${SERVICE_DIR}"
    python3 - "${data}" <<'PY'
import sys
payload = (b"zstd functional roundtrip sample " * 8192) + bytes(range(256)) * 64
with open(sys.argv[1], "wb") as handle:
    handle.write(payload)
PY
    "${ZSTD_BIN}" -q -f -1 "${data}" -o "${packed}" || return 40
    "${ZSTD_BIN}" -q -f -d "${packed}" -o "${unpacked}" || return 40
    cmp -s "${data}" "${unpacked}" || return 40
}

# 运行上游 playTests.sh 短测;需要 ZSTD_BIN 与 DATAGEN_BIN 指向被测二进制与数据生成器。
run_upstream_suite() {
    local tree="$1"
    [[ -f "${tree}/tests/playTests.sh" ]] || return 1
    (
        cd "${tree}/tests"
        ZSTD_BIN="${ZSTD_BIN}" DATAGEN_BIN="${ZSTD_DATAGEN_BIN}" sh "${ZSTD_PLAY_TESTS}"
    )
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" u_status="$4" u_detail="$5" c_status="$6" c_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_U_STATUS="${u_status}" FUNC_U_DETAIL="${u_detail}" \
        FUNC_C_STATUS="${c_status}" FUNC_C_DETAIL="${c_detail}" \
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
        {"name": "upstream-test", "status": env("FUNC_U_STATUS"), "detail": env("FUNC_U_DETAIL")},
        {"name": "cli-roundtrip", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    : >"${RESULTS_DIR}/zstd_test.log"
    [[ -x "${ZSTD_BIN}" ]] || {
        log "ERROR: zstd CLI is unavailable"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local roundtrip_status="failed" roundtrip_detail=""
    local actual tree

    # 1) 版本
    actual="$(read_zstd_version "${ZSTD_BIN}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="v=${actual}"
    else
        version_detail="zstd ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游 playTests.sh 短测(源码树与 datagen 由 install 阶段准备)
    tree="${ZSTD_TEST_TREE}"
    if [[ ! -f "${tree}/tests/playTests.sh" || ! -x "${ZSTD_DATAGEN_BIN}" ]]; then
        upstream_detail="upstream test tree or datagen not found (tree=${tree}, datagen=${ZSTD_DATAGEN_BIN})"
    elif run_upstream_suite "${tree}" >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="upstream 'sh tests/${ZSTD_PLAY_TESTS}' passed (source: ${tree})"
    else
        upstream_detail="upstream playTests.sh failed; see upstream_test.log"
    fi

    # 3) 内部 smoke:CLI 回环
    if run_cli_roundtrip >"${RESULTS_DIR}/cli_roundtrip.log" 2>&1; then
        roundtrip_status="passed"
        roundtrip_detail="compress/decompress roundtrip ok (sha256 matched)"
    else
        roundtrip_detail="compress/decompress roundtrip failed; see cli_roundtrip.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${roundtrip_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${roundtrip_status}" "${roundtrip_detail}"
    log_line "checks: version=${version_status} upstream=${upstream_status} roundtrip=${roundtrip_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${SERVICE_DIR}" ]] && rm -rf -- "${SERVICE_DIR}"
    log "cleaned zstd task data"
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
Run the zstd functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    zstd version (default: ${SOFTWARE_VERSION})
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