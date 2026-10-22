#!/usr/bin/env bash
#
# oceanbase_test.sh - OceanBase 性能评估适配脚本（openEuler BoostKit 性能评估框架）。
#
# 实现四个阶段的 shell 函数，供框架（command_adapter）按阶段调用：
#   build_oceanbase         构建/部署环境与 OceanBase 组件（obd demo 部署 observer）
#   start_oceanbase_service 启动/确认 OceanBase observer 服务可用
#   run_oceanbase_benchmarks 执行性能基准测试并聚合结果
#   stop_oceanbase_service  停止并销毁 OceanBase 测试集群、清理资源
#
# 同时支持脱离项目根目录的独立执行（source 本文件不会运行任何内容）：
#   ./oceanbase_test.sh [--version 5.0.1.0] [--results-dir DIR] [--keep-workdir]
#
# 部署说明：OceanBase 属于“部署服务型”软件（区别于源码编译型，如 RocksDB），
# build 阶段使用 OceanBase Deployer（obd）的 demo 命令在任务私有目录内部署单机
# 演示集群，并通过独立端口与独立 OBD_HOME 保证任务隔离；start/test 阶段通过
# mysql 协议连接 observer 执行 sysbench OLTP 与微基准测试。

set -euo pipefail

# ---------------------------------------------------------------------------
# 公共变量（可被框架环境变量覆盖）
# ---------------------------------------------------------------------------
SOFTWARE_NAME="${SOFTWARE_NAME:-oceanbase}"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-5.0.1.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
EXPECTED_ARCH="${EXPECTED_ARCH/x86_64/x86_64}"
EXPECTED_ARCH="${EXPECTED_ARCH/amd64/x86_64}"
EXPECTED_ARCH="${EXPECTED_ARCH/aarch64/aarch64}"
EXPECTED_ARCH="${EXPECTED_ARCH/arm64/aarch64}"

PERF_RUN_ID="${PERF_RUN_ID:-local-$(date +%Y%m%d-%H%M%S)}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# OceanBase 运行/连接配置
OB_HOST="127.0.0.1"
OB_PORT="${OB_PORT:-}"
OB_RPC_PORT="${OB_RPC_PORT:-}"
OB_USER="${OB_USER:-root}"
OB_PASSWORD="${OB_PASSWORD:-}"
OB_DB="${OB_DB:-sbtest}"

# 基准测试参数
TABLES="${TABLES:-4}"
TABLE_SIZE="${TABLE_SIZE:-10000}"
TIME_PER_TEST="${TIME_PER_TEST:-60}"
ITERATIONS="${ITERATIONS:-1}"

# 部署相关路径
OB_DEPLOY_HOME=""
OBD_HOME=""

# 独立执行状态标记
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() {
    printf '%s\n' "[oceanbase] $*"
}

# ---------------------------------------------------------------------------
# 路径与运行时配置
# ---------------------------------------------------------------------------
configure_runtime_paths() {
    local normalized_run_id
    normalized_run_id="$(printf '%s' "${PERF_RUN_ID}" | tr -c 'A-Za-z0-9._-' '_')"

    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${normalized_run_id}"
    fi

    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/oceanbase/local-${normalized_run_id}"
        STANDALONE_OWNS_WORK_DIR=1
    fi

    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    OB_DEPLOY_HOME="${PERF_WORK_DIR}/oceanbase-demo"
    OBD_HOME="${PERF_WORK_DIR}/obd-meta"

    export SOFTWARE_NAME
    export SOFTWARE_VERSION
    export EXPECTED_ARCH
    export OBD_HOME
    export TMPDIR
}

compute_oceanbase_ports() {
    local checksum
    if [[ -z "${OB_PORT}" ]]; then
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        OB_PORT="$((20000 + ${checksum%% *} % 20000))"
        if (( OB_PORT < 1024 || OB_PORT > 65535 )); then
            log "ERROR: derived invalid port ${OB_PORT}"
            return 20
        fi
    fi
    if [[ -z "${OB_RPC_PORT}" ]]; then
        OB_RPC_PORT="$((OB_PORT + 20000))"
        if (( OB_RPC_PORT < 1024 || OB_RPC_PORT > 65535 )); then
            log "ERROR: derived invalid rpc port ${OB_RPC_PORT}"
            return 20
        fi
    fi
}

initialize_runtime() {
    configure_runtime_paths || return $?
    compute_oceanbase_ports || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# ---------------------------------------------------------------------------
# 数据库操作辅助函数
# ---------------------------------------------------------------------------
ob_mysql() {
    local args
    args=("mysql" "-h${OB_HOST}" "-P${OB_PORT}" "-u${OB_USER}")
    if [[ -n "${OB_PASSWORD}" ]]; then
        args+=("-p${OB_PASSWORD}")
    fi
    "${args[@]}" "$@"
}

check_observer_ready() {
    ob_mysql -N -e "SELECT 1" >/dev/null 2>&1
}

query_observer_version() {
    local value
    value="$(ob_mysql -N -s -e "SELECT version()" 2>/dev/null || true)"
    printf '%s' "${value}" | tr -d '\r\n\t'
}

# ---------------------------------------------------------------------------
# 依赖安装
# ---------------------------------------------------------------------------
write_oceanbase_repo() {
    local repo_file="/etc/yum.repos.d/oceanbase.repo" arch
    if [[ -f "${repo_file}" ]]; then
        return 0
    fi
    arch="$(uname -m)"
    if [[ "$(id -u)" -eq 0 ]]; then
        printf '[oceanbase]\nname=OceanBase Community el8\nbaseurl=https://mirrors.oceanbase.com/community/stable/el/8/%s/\nenabled=1\ngpgcheck=0\n' \
            "${arch}" > "${repo_file}"
    elif command -v sudo >/dev/null 2>&1; then
        printf '[oceanbase]\nname=OceanBase Community el8\nbaseurl=https://mirrors.oceanbase.com/community/stable/el/8/%s/\nenabled=1\ngpgcheck=0\n' \
            "${arch}" | sudo tee "${repo_file}" >/dev/null
    else
        return 1
    fi
}

install_oceanbase_dependencies() {
    local packages=() package

    for package in python3 curl; do
        command -v "${package}" >/dev/null 2>&1 || packages+=("${package}")
    done

    if ! command -v obd >/dev/null 2>&1; then
        if ! rpm -q ob-deploy >/dev/null 2>&1; then
            write_oceanbase_repo || {
                log "ERROR: cannot configure the OceanBase package repository"
                return 30
            }
            packages+=("ob-deploy")
        fi
    fi

    command -v mysql >/dev/null 2>&1 || packages+=("mysql")
    command -v sysbench >/dev/null 2>&1 || packages+=("sysbench")

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing OceanBase test dependencies: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        if ! dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
            log "ERROR: dnf install failed for ${packages[*]}"
            return 30
        fi
    elif command -v sudo >/dev/null 2>&1; then
        if ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
            log "ERROR: sudo dnf install failed for ${packages[*]}"
            return 30
        fi
    else
        log "ERROR: cannot install required OceanBase dependencies without root/sudo"
        return 30
    fi
}

# ---------------------------------------------------------------------------
# 阶段 1：build - 部署 OceanBase observer
# ---------------------------------------------------------------------------
build_oceanbase() {
    local runner_architecture actual_version attempt

    initialize_runtime || return $?
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    install_oceanbase_dependencies || return $?

    if check_observer_ready; then
        log "ERROR: an OceanBase observer is already reachable on the task port ${OB_HOST}:${OB_PORT}"
        log "ERROR: refusing to benchmark a pre-existing service"
        return 20
    fi

    log "deploying OceanBase ${SOFTWARE_VERSION} observer via obd demo (task-private)"
    mkdir -p "${OBD_HOME}" "${OB_DEPLOY_HOME}"
    if ! obd demo -c oceanbase-ce \
            "--oceanbase-ce.version=${SOFTWARE_VERSION}" \
            "--oceanbase-ce.mysql_port=${OB_PORT}" \
            "--oceanbase-ce.rpc_port=${OB_RPC_PORT}" \
            "--home_path=${OB_DEPLOY_HOME}"; then
        log "ERROR: obd demo deployment failed"
        return 40
    fi

    log "waiting for the OceanBase observer to become ready"
    for attempt in {1..120}; do
        if check_observer_ready; then
            log "observer ready on ${OB_HOST}:${OB_PORT} (after ${attempt} checks)"
            break
        fi
        sleep 3
    done
    if ! check_observer_ready; then
        log "ERROR: observer did not become ready on ${OB_HOST}:${OB_PORT}"
        return 40
    fi

    actual_version="$(query_observer_version)"
    actual_version="$(printf '%s' "${actual_version}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: deployed observer reports ${actual_version:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    fi

    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "OceanBase ${actual_version} observer deployed"
}

# ---------------------------------------------------------------------------
# 阶段 2：start - 确认服务可用
# ---------------------------------------------------------------------------
start_oceanbase_service() {
    initialize_runtime || return $?
    if ! check_observer_ready; then
        log "ERROR: OceanBase observer is not reachable on ${OB_HOST}:${OB_PORT}"
        return 40
    fi
    log "OceanBase observer already running on ${OB_HOST}:${OB_PORT}"
    return 0
}

# ---------------------------------------------------------------------------
# 阶段 3：test - 执行基准测试并聚合结果
# ---------------------------------------------------------------------------
run_oceanbase_benchmarks() {
    initialize_runtime || return $?
    if ! check_observer_ready; then
        log "ERROR: OceanBase observer is not reachable on ${OB_HOST}:${OB_PORT}"
        return 40
    fi

    mkdir -p "${RESULTS_DIR}"

    log "running OLTP benchmark (sysbench)"
    python3 "${SCRIPT_DIR}/scripts/benchmark_ob.py" \
        "${RESULTS_DIR}/benchmark_ob.json" \
        "${OB_HOST}" "${OB_PORT}" "${OB_USER}" "${OB_PASSWORD}" "${OB_DB}" \
        "${TABLES}" "${TABLE_SIZE}" "${ITERATIONS}" "${TIME_PER_TEST}" \
        || { log "ERROR: OLTP benchmark failed"; return 40; }

    log "running micro benchmark (thread/table scaling)"
    python3 "${SCRIPT_DIR}/scripts/micro_benchmark.py" \
        "${RESULTS_DIR}/micro_benchmark.json" \
        "${OB_HOST}" "${OB_PORT}" "${OB_USER}" "${OB_PASSWORD}" \
        "${TABLES}" "${TABLE_SIZE}" "${ITERATIONS}" "${TIME_PER_TEST}" \
        || { log "ERROR: micro benchmark failed"; return 40; }

    log "aggregating benchmark results"
    python3 "${SCRIPT_DIR}/scripts/aggregate_results.py" \
        "${RESULTS_DIR}" "${RESULTS_DIR}/results.json" \
        || { log "ERROR: results aggregation failed"; return 40; }

    log "benchmarks completed; results written to ${RESULTS_DIR}"
}

# ---------------------------------------------------------------------------
# 阶段 4：stop - 销毁集群并清理资源
# ---------------------------------------------------------------------------
stop_oceanbase_service() {
    initialize_runtime || return $?
    if command -v obd >/dev/null 2>&1; then
        export OBD_HOME
        if ! obd cluster destroy demo -f; then
            log "WARN: obd cluster destroy reported an error; continuing with cleanup"
        fi
    else
        log "obd unavailable; skipping cluster teardown"
    fi
    rm -rf "${OB_DEPLOY_HOME}"
    log "OceanBase observer removed from ${OB_DEPLOY_HOME}"
}

# ---------------------------------------------------------------------------
# 独立执行
# ---------------------------------------------------------------------------
cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping standalone work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log "external work directory was not removed: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/oceanbase/local-* || "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/oceanbase" ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}" || return 70
    log "cleaned standalone work directory: ${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_oceanbase_service
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_oceanbase_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed"

    initialize_runtime || return $?
    trap 'emergency_standalone_cleanup' EXIT

    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" system "${RESULTS_DIR}/system_info.json" \
        || { stage_status=$?; failed_stage="prepare"; }
    if [[ "${stage_status}" -eq 0 ]]; then
        python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" runtime "${RESULTS_DIR}/runtime_before.json" \
            || { stage_status=$?; failed_stage="prepare"; }
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        build_oceanbase || { stage_status=$?; failed_stage="build"; }
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" build-info \
            "${RESULTS_DIR}/build_info.json" "${SOFTWARE_VERSION}" "${PERF_ACTUAL_VERSION_FILE}" \
            "${EXPECTED_ARCH}" "${PERF_RUN_ID}" || { stage_status=$?; failed_stage="build"; }
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        start_oceanbase_service || { stage_status=$?; failed_stage="start"; }
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        run_oceanbase_benchmarks || { stage_status=$?; failed_stage="test"; }
    fi

    stop_oceanbase_service || cleanup_status="failed"
    STANDALONE_STOP_DONE=1
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" runtime "${RESULTS_DIR}/runtime_after.json" \
        || cleanup_status="failed"
    if [[ "${stage_status}" -eq 0 ]]; then
        python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" finalize \
            "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" "${PERF_RUN_ID}" \
            passed "${cleanup_status}" "" || stage_status=$?
    else
        python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" finalize \
            "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" "${PERF_RUN_ID}" \
            failed "${cleanup_status}" "${failed_stage}" || true
    fi

    cleanup_standalone_workdir || stage_status=$?
    STANDALONE_CLEANUP_DONE=1
    trap - EXIT

    return "${stage_status}"
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir)
                RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir)
                STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help)
                printf 'Usage: %s [--version VERSION] [--results-dir DIR] [--keep-workdir]\n' "$(basename "$0")"
                return 0 ;;
            *)
                log "ERROR: unsupported option: $1"
                return 10 ;;
        esac
    done
    run_oceanbase_standalone
}

# 受保护的独立执行入口：仅当被直接执行时才进入 main；被 framework source 时
# 仅定义函数，不执行任何入口逻辑。
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
