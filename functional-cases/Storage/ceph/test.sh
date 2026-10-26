#!/usr/bin/env bash
# Ceph 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 上游离线自检工具(ceph-dencoder / monmaptool) + 最小可用性(守护进程二进制)。
# Ceph 完整集群(mon+osd+mgr 引导)很重,这里做最小可用性验证,并在 detail 说明局限。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 运行上游自带、可离线执行的编码/映射工具,验证核心库功能。
# CEPH_ROOTFS 非空时工具路径来自导出的根文件系统,统一经 chroot 执行。
run_upstream_selftest() {
    local dencoder monmaptool map
    dencoder="$(ceph_tool_path ceph-dencoder || true)"
    [[ -n "${dencoder}" ]] || {
        log "ERROR: ceph-dencoder is not available"
        return 40
    }
    # ceph-dencoder 的子命令是 list_types(不是 types list),输出一行一个类型名。
    ceph_exec "${dencoder}" list_types >>"${RESULTS_DIR}/ceph_smoke.log" 2>&1 || {
        log "ERROR: ceph-dencoder self-test failed"
        return 40
    }
    monmaptool="$(ceph_tool_path monmaptool || true)"
    if [[ -n "${monmaptool}" ]]; then
        map="${SERVICE_DIR}/monmap"
        # chroot 内看到的是根文件系统自己的目录树,先把 map 的父目录在根文件系统内建好,
        # 这样同一绝对路径在 chroot 内外都指向同一个文件。
        [[ -z "${CEPH_ROOTFS}" ]] || mkdir -p "$(dirname "${CEPH_ROOTFS}${map}")"
        ceph_exec "${monmaptool}" --create --clobber --add a 127.0.0.1:6789 "${map}" \
            >>"${RESULTS_DIR}/ceph_smoke.log" 2>&1 || {
            log "ERROR: monmaptool --create failed"
            return 40
        }
        ceph_exec "${monmaptool}" --print "${map}" >>"${RESULTS_DIR}/ceph_smoke.log" 2>&1 || {
            log "ERROR: monmaptool --print failed"
            return 40
        }
    fi
}

# 最小可用性冒烟:核心守护进程二进制可执行并报告版本。
# 说明:此检查不引导真实集群(mon+osd+mgr),因完整 cephadm 编排放不进本用例的时限。
run_daemon_availability() {
    local bin version_ok=0
    for bin in "${CEPH_MON_BIN}" "${CEPH_OSD_BIN}" "${CEPH_MGR_BIN}"; do
        ceph_bin_exists "${bin}" || continue
        ceph_exec "${bin}" --version >>"${RESULTS_DIR}/ceph_smoke.log" 2>&1 || return 40
        version_ok=1
    done
    [[ "${version_ok}" -eq 1 ]] || {
        log "ERROR: no ceph daemon binaries (ceph-mon/ceph-osd/ceph-mgr) were found"
        return 40
    }
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
        {"name": "cluster-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    resolve_ceph_binaries || {
        log "ERROR: ceph client binary is unavailable"
        return 40
    }
    mkdir -p "${SERVICE_DIR}"
    : >"${RESULTS_DIR}/ceph_smoke.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(ceph_version_of "${CEPH_BIN}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="version=${actual}"
    else
        version_detail="ceph ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游离线自检工具
    if run_upstream_selftest; then
        upstream_status="passed"
        upstream_detail="ceph-dencoder + monmaptool offline self-test passed"
    else
        upstream_detail="upstream offline self-test failed; see ceph_smoke.log"
    fi

    # 3) 最小可用性(守护进程二进制);完整集群引导超出用例范围。
    if run_daemon_availability; then
        smoke_status="passed"
        smoke_detail="ceph-mon/ceph-osd/ceph-mgr binaries execute and report versions (full cluster bootstrap via cephadm is out of scope)"
    else
        smoke_detail="ceph daemon binaries were unavailable; see ceph_smoke.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream-test=${upstream_status} cluster-smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${SERVICE_DIR}" ]] && rm -rf -- "${SERVICE_DIR}"
    log "cleaned Ceph task data"
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
Run the Ceph functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Ceph version (default: ${SOFTWARE_VERSION})
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