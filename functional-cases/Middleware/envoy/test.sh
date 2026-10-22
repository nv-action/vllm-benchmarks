#!/usr/bin/env bash
# Envoy 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 上游配置校验(--mode validate) + 启动实例的 admin/listener 探活。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 生成一份最小静态配置:HTTP 直答 + admin 接口,用于配置校验与探活。
write_smoke_config() {
    mkdir -p "${RUNTIME_DIR}" || return 40
    cat >"${RUNTIME_DIR}/smoke.yaml" <<EOF
admin:
  address:
    socket_address: { address: 127.0.0.1, port_value: ${ENVOY_ADMIN_PORT} }
static_resources:
  listeners:
  - name: main
    address:
      socket_address: { address: 127.0.0.1, port_value: ${ENVOY_LISTENER_PORT} }
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: ingress
          route_config:
            name: local
            virtual_hosts:
            - name: local
              domains: ["*"]
              routes:
              - match: { prefix: "/" }
                direct_response:
                  status: 200
                  body: { inline_string: "ok" }
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
EOF
}

# 上游配置校验:envoy --mode validate -c <config>。
run_config_validate() {
    "${ENVOY_BIN}" --mode validate -c "${RUNTIME_DIR}/smoke.yaml" \
        --log-path "${RUNTIME_DIR}/validate.log"
}

# 启动 envoy 实例,探活 admin /ready 与 listener 直答。
run_proxy_smoke() {
    local pid=0 attempt
    : >"${RUNTIME_DIR}/instance.log"
    "${ENVOY_BIN}" -c "${RUNTIME_DIR}/smoke.yaml" \
        --log-path "${RUNTIME_DIR}/instance.log" --base-id 1 --disable-hot-restart \
        >/dev/null 2>&1 &
    pid=$!
    printf '%s\n' "${pid}" >"${RUNTIME_DIR}/envoy.pid"
    for attempt in {1..30}; do
        kill -0 "${pid}" 2>/dev/null || break
        if curl --noproxy '*' -fsS --max-time 1 \
            "http://127.0.0.1:${ENVOY_ADMIN_PORT}/ready" 2>/dev/null | grep -q LIVE; then
            break
        fi
        sleep 1
    done
    if ! kill -0 "${pid}" 2>/dev/null; then
        log "ERROR: envoy instance exited early; see ${RUNTIME_DIR}/instance.log"
        return 40
    fi
    if ! curl --noproxy '*' -fsS --max-time 2 \
        "http://127.0.0.1:${ENVOY_ADMIN_PORT}/ready" 2>/dev/null | grep -q LIVE; then
        log "ERROR: envoy admin /ready did not report LIVE"
        return 40
    fi
    local body
    body="$(curl --noproxy '*' -fsS --max-time 2 "http://127.0.0.1:${ENVOY_LISTENER_PORT}/" 2>/dev/null || true)"
    kill -TERM "${pid}" 2>/dev/null || true
    for attempt in {1..10}; do
        kill -0 "${pid}" 2>/dev/null || break
        sleep 1
    done
    kill -KILL "${pid}" 2>/dev/null || true
    rm -f "${RUNTIME_DIR}/envoy.pid"
    [[ "${body}" == "ok" ]] || {
        log "ERROR: envoy listener did not answer 'ok' (got '${body}')"
        return 40
    }
    return 0
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
        {"name": "proxy-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    [[ -x "${ENVOY_BIN}" ]] || {
        log "ERROR: envoy binary is unavailable"
        return 40
    }
    write_smoke_config || return $?

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(read_envoy_version || true)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="version=${actual}"
    else
        version_detail="envoy ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游配置校验(--mode validate)
    if run_config_validate >"${RESULTS_DIR}/upstream_validate.log" 2>&1 &&
        grep -qi 'OK' "${RESULTS_DIR}/upstream_validate.log"; then
        upstream_status="passed"
        upstream_detail="envoy --mode validate reported OK"
    else
        upstream_detail="envoy --mode validate failed; see upstream_validate.log"
    fi

    # 3) 启动实例 smoke:admin /ready + listener 直答
    if run_proxy_smoke >"${RESULTS_DIR}/proxy_smoke.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="admin /ready LIVE and listener returned 200 ok"
    else
        smoke_detail="proxy smoke failed; see proxy_smoke.log and ${RUNTIME_DIR}/instance.log"
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
        printf 'envoy functional check (version %s, mode %s)\n' "${SOFTWARE_VERSION}" "${FUNC_MODE}"
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
    if [[ -f "${RUNTIME_DIR}/envoy.pid" ]]; then
        pid="$(<"${RUNTIME_DIR}/envoy.pid")"
    fi
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        kill -TERM "${pid}" 2>/dev/null || true
        sleep 1
        kill -KILL "${pid}" 2>/dev/null || true
    fi
    [[ -d "${RUNTIME_DIR}" ]] && rm -rf -- "${RUNTIME_DIR}"
    log "cleaned envoy task data"
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
Run the envoy functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Envoy version (default: ${SOFTWARE_VERSION})
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