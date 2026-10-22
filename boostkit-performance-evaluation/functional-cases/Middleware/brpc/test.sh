#!/usr/bin/env bash
# brpc 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 上游 example/echo_c++ 回环 + 最小链接 smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# client 失败/超时后的诊断证据:server 进程状态、/status 实际响应、server 日志尾部。
# (brpc RPC 在对端"已监听但不响应"时默认无限等待,曾经把整个 check 挂死 18 分钟,
# 这些证据用于区分 server 楔死 / client 单侧问题。)
dump_echo_diagnostics() {
    local server_pid="$1" rc="$2"
    {
        printf 'echo client got no usable response (rc=%s; 124 = killed by the 20s timeout)\n' "${rc}"
        printf '\n=== server process state:\n'
        ps -o pid,stat,etime,args -p "${server_pid}" 2>/dev/null ||
            printf 'server process %s is gone\n' "${server_pid}"
        printf '\n=== server /status response:\n'
        curl --noproxy '*' -fsS --max-time 3 "http://127.0.0.1:${BRPC_ECHO_PORT}/status" 2>&1 ||
            printf '(unreachable)\n'
        printf '\n=== server log tail:\n'
        tail -n 40 "${SERVICE_DIR}/echo_server.log" 2>/dev/null
    } >>"${RESULTS_DIR}/upstream_echo.log" 2>&1 || true
}

# 运行上游 example/echo_c++ 的 server/client 做一次 RPC 回环。
run_upstream_echo() {
    local server_pid=0 attempt rc=0 client_failed=0
    [[ -x "${BRPC_ECHO_SERVER_BIN}" && -x "${BRPC_ECHO_CLIENT_BIN}" ]] || {
        log "ERROR: upstream echo example binaries are unavailable"
        return 40
    }
    mkdir -p "${SERVICE_DIR}"
    "${BRPC_ECHO_SERVER_BIN}" --port="${BRPC_ECHO_PORT}" \
        >"${SERVICE_DIR}/echo_server.log" 2>&1 &
    server_pid=$!
    printf '%s\n' "${server_pid}" >"${SERVICE_DIR}/echo_server.pid"
    for attempt in {1..60}; do
        kill -0 "${server_pid}" 2>/dev/null || break
        if curl --noproxy '*' -fsS --max-time 1 -o /dev/null \
            "http://127.0.0.1:${BRPC_ECHO_PORT}/status" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if ! kill -0 "${server_pid}" 2>/dev/null; then
        log "ERROR: echo server exited early; see ${SERVICE_DIR}/echo_server.log"
        return 40
    fi
    # 上游 echo 客户端是 while (!brpc::IsAskedToQuit()) 常驻循环,每秒发一次请求,
    # 不会自行退出,必须用 timeout 主动结束;判定依据改为日志中是否出现成功响应。
    # 同时保留硬超时兜底:brpc RPC 在对端"已监听但不响应"时默认无限等待。
    timeout -s TERM 20 "${BRPC_ECHO_CLIENT_BIN}" --server="127.0.0.1:${BRPC_ECHO_PORT}" \
        >"${SERVICE_DIR}/echo_client.log" 2>&1 || rc=$?
    # 正常路径下 rc=124(客户端被 timeout 结束)属于预期,不能据此判定失败。
    grep -q 'Received response from' "${SERVICE_DIR}/echo_client.log" || client_failed=1
    [[ "${client_failed}" -eq 0 ]] || dump_echo_diagnostics "${server_pid}" "${rc}"
    kill -TERM "${server_pid}" 2>/dev/null || true
    # 限时等待退出;吞信号不退时强杀,避免裸 wait 无限阻塞。
    for attempt in {1..10}; do
        kill -0 "${server_pid}" 2>/dev/null || break
        sleep 1
    done
    if kill -0 "${server_pid}" 2>/dev/null; then
        kill -KILL "${server_pid}" 2>/dev/null || true
        sleep 1
    fi
    wait "${server_pid}" 2>/dev/null || true
    rm -f "${SERVICE_DIR}/echo_server.pid"
    [[ "${client_failed}" -eq 0 ]] || {
        log "ERROR: echo client received no response (rc=${rc}); see upstream_echo.log and echo_client.log"
        return 40
    }
    return 0
}

# 运行最小链接 smoke 程序(仅验证头文件与 libbrpc 可用)。
run_link_smoke() {
    [[ -x "${BRPC_LINK_SMOKE_BIN}" ]] || {
        log "ERROR: minimal smoke binary is unavailable"
        return 40
    }
    timeout 120 "${BRPC_LINK_SMOKE_BIN}" >"${RESULTS_DIR}/link_smoke.log" 2>&1
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
        {"name": "cpp-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    mkdir -p "${SERVICE_DIR}"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(detect_brpc_version || true)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="RELEASE_VERSION=${actual}"
    else
        version_detail="brpc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游 example/echo_c++ 回环
    if run_upstream_echo >"${RESULTS_DIR}/upstream_echo.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="upstream example/echo_c++ roundtrip ok on port ${BRPC_ECHO_PORT}"
    else
        upstream_detail="upstream example/echo_c++ roundtrip failed; see upstream_echo.log"
    fi

    # 3) 最小链接 smoke
    if run_link_smoke; then
        smoke_status="passed"
        smoke_detail="minimal program linked libbrpc and ran successfully"
    else
        smoke_detail="minimal link smoke failed; see link_smoke.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    {
        printf 'brpc functional check (version %s, mode %s)\n' "${SOFTWARE_VERSION}" "${FUNC_MODE}"
        printf 'version:  %s - %s\n' "${version_status}" "${version_detail}"
        printf 'upstream: %s - %s\n' "${upstream_status}" "${upstream_detail}"
        printf 'smoke:    %s - %s\n' "${smoke_status}" "${smoke_detail}"
        printf 'overall:  %s\n' "${overall}"
    } >"${RESULTS_DIR}/check.log"
    log "checks: version=${version_status} upstream=${upstream_status} smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    local pid=""
    if [[ -f "${SERVICE_DIR}/echo_server.pid" ]]; then
        pid="$(<"${SERVICE_DIR}/echo_server.pid")"
    fi
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        kill -TERM "${pid}" 2>/dev/null || true
        sleep 1
        kill -KILL "${pid}" 2>/dev/null || true
    fi
    [[ -d "${SERVICE_DIR}" ]] && rm -rf -- "${SERVICE_DIR}"
    log "cleaned brpc task data"
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
Run the brpc functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    brpc version (default: ${SOFTWARE_VERSION})
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