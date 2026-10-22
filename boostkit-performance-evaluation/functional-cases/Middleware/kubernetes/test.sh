#!/usr/bin/env bash
# Kubernetes 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + kubectl 离线生成 Deployment + 内建资源生成 smoke。
# 无需真实集群:只使用完全在本地构造对象的 kubectl create 生成器,不触发 API discovery。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 上游客户端功能:用 kubectl 内建生成器离线渲染 Deployment 并校验关键字段。
# 注意:apply/create -f 的 --dry-run=client 仍需要 API discovery,没有集群时会
# 回落到 localhost:8080,因此这里只用完全在本地构造对象的生成器命令。
run_upstream_generate() {
    local generated="${KUBECTL_WORK_DIR}/deployment.yaml"
    mkdir -p "${KUBECTL_WORK_DIR}" || return 40
    "${KUBECTL_BIN}" create deployment func-smoke \
        --image=nginx:alpine --dry-run=client -o yaml >"${generated}" || return 40
    grep -q 'kind: Deployment' "${generated}" || return 41
    grep -q 'name: func-smoke' "${generated}" || return 41
    grep -q 'image: nginx:alpine' "${generated}" || return 41
}

# 基础 smoke:用 kubectl create 生成 ConfigMap 与 Job 的 YAML,校验类型与名称。
run_kubectl_smoke() {
    local generated="${KUBECTL_WORK_DIR}/generated.yaml"
    "${KUBECTL_BIN}" create configmap func-smoke \
        --from-literal=key=value --dry-run=client -o yaml >"${generated}" || return 40
    grep -q 'kind: ConfigMap' "${generated}" || return 41
    grep -q 'name: func-smoke' "${generated}" || return 41
    "${KUBECTL_BIN}" create job func-smoke \
        --image=busybox --dry-run=client -o yaml >"${generated}" || return 40
    grep -q 'kind: Job' "${generated}" || return 41
    grep -q 'name: func-smoke' "${generated}" || return 41
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
        {"name": "kubectl-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    [[ -x "${KUBECTL_BIN}" ]] || {
        log "ERROR: kubectl client is unavailable"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(read_kubectl_version || true)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="client version=v${actual}"
    else
        version_detail="kubectl client ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游客户端功能:离线渲染 Deployment
    if run_upstream_generate >"${RESULTS_DIR}/upstream_generate.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="kubectl create deployment --dry-run=client rendered a valid manifest"
    else
        upstream_detail="kubectl create deployment --dry-run=client failed; see upstream_generate.log"
    fi

    # 3) 基础 smoke:ConfigMap + Job 生成
    if run_kubectl_smoke >"${RESULTS_DIR}/kubectl_smoke.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="kubectl create rendered ConfigMap and Job manifests"
    else
        smoke_detail="kubectl create smoke failed; see kubectl_smoke.log"
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
        printf 'kubernetes functional check (version %s, mode %s)\n' "${SOFTWARE_VERSION}" "${FUNC_MODE}"
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
    [[ -d "${KUBECTL_WORK_DIR}" ]] && rm -rf -- "${KUBECTL_WORK_DIR}"
    log "cleaned kubernetes task data"
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
Run the kubernetes functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Kubernetes release version (default: ${SOFTWARE_VERSION})
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