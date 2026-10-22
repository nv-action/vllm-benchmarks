#!/usr/bin/env bash
# Redis 测试脚本:功能判定 + 收尾。
# 判定优先复用上游 Tcl 测试套件,叠加版本校验与 CLI SET/GET 回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

run_cli_roundtrip() {
    "${REDIS_SERVER_BIN}" \
        --bind 127.0.0.1 --protected-mode no \
        --port "${REDIS_SERVICE_PORT}" --dir "${SERVICE_DIR}" \
        --daemonize yes --pidfile "${PID_FILE}" --logfile "${LOG_FILE}" || return 40

    local attempt
    for attempt in {1..30}; do
        if "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING 2>/dev/null | grep -qx PONG; then
            break
        fi
        sleep 1
    done
    "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING | grep -qx PONG || return 40
    "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" SET func_key func_value >/dev/null || return 40
    local got
    got="$("${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" GET func_key)"
    [[ "${got}" == "func_value" ]] || return 40
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
    [[ -x "${REDIS_SERVER_BIN}" && -x "${REDIS_CLI_BIN}" ]] || {
        log "ERROR: redis binaries are unavailable"
        return 40
    }
    mkdir -p "${SERVICE_DIR}"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local cli_status="failed" cli_detail=""
    local actual source_dir

    # 1) 版本
    actual="$("${REDIS_SERVER_BIN}" --version | sed -n 's/.*v=\([^ ]*\).*/\1/p' | head -n 1)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="v=${actual}"
    else
        version_detail="redis ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游测试套件(测试树由 install 阶段准备:prebuilt=浅克隆测试脚本+软链二进制,build=本次克隆目录)
    source_dir="$(resolve_redis_source_dir || true)"
    if [[ -z "${source_dir}" ]]; then
        upstream_detail="test tree with ./runtest not found (searched REDIS_TEST_TREE=${REDIS_TEST_TREE:-unset}, REDIS_SOURCE_DIR=${REDIS_SOURCE_DIR:-unset}); did the install stage clone the test suite?"
    elif (cd "${source_dir}" && ./runtest --single "${REDIS_UPSTREAM_TEST}" --clients 5) \
        >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="upstream suite '${REDIS_UPSTREAM_TEST}' passed (source: ${source_dir})"
    else
        upstream_detail="upstream suite '${REDIS_UPSTREAM_TEST}' failed; see upstream_test.log"
    fi

    # 3) 内部 smoke:CLI 回环
    if run_cli_roundtrip >"${RESULTS_DIR}/cli_roundtrip.log" 2>&1; then
        cli_status="passed"
        cli_detail="SET/GET roundtrip ok on port ${REDIS_SERVICE_PORT}"
    else
        cli_detail="SET/GET roundtrip failed; see cli_roundtrip.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${cli_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${cli_status}" "${cli_detail}"
    log "checks: version=${version_status} upstream=${upstream_status} cli=${cli_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    if [[ -x "${REDIS_CLI_BIN}" ]]; then
        "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" SHUTDOWN NOSAVE >/dev/null 2>&1 || true
    fi
    local pid=""
    if [[ -f "${PID_FILE}" ]]; then
        pid="$(<"${PID_FILE}")"
    fi
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        kill -TERM "${pid}" 2>/dev/null || true
        sleep 1
        kill -KILL "${pid}" 2>/dev/null || true
    fi
    [[ -d "${SERVICE_DIR}" ]] && rm -rf -- "${SERVICE_DIR}"
    # 删除本次运行的短路径工作树(仅限约定前缀,避免误删)。
    if [[ -n "${REDIS_RUN_ROOT:-}" && "${REDIS_RUN_ROOT}" == /tmp/redis-func-* && -d "${REDIS_RUN_ROOT}" ]]; then
        rm -rf -- "${REDIS_RUN_ROOT}"
    fi
    log "cleaned redis task data"
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
Run the redis functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Redis version (default: ${SOFTWARE_VERSION})
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