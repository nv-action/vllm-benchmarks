#!/usr/bin/env bash
# OceanBase 构建脚本:环境采集 + 准备被测软件(分层获取 observer 或校验镜像)。
#   FUNC_MODE=prebuilt - 镜像内已解包好 observer:启动镜像自带实例(2881)并校验版本
#   FUNC_MODE=build    - 分层优先获取:官方 rpm 解包直装 → 三方仓库 → 源码编译
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version memory_mb
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    memory_mb="$(awk '/MemTotal/ {printf "%d", $2/1024}' /proc/meminfo 2>/dev/null || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "memory_mb": ${memory_mb:-0}
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------
#
# build 模式采用“分层优先获取”:每层取得 observer 后必须校验 SELECT version()
# == SOFTWARE_VERSION,不匹配或获取失败则降级到下一层,三层都不可行才让 install 失败:
#   1) official-binary 上游官方二进制:下载官方 oceanbase-ce rpm,按包内布局解包到 /,
#      再用包自带的 profile/oceanbase-service.sh 启动 —— 与 openEuler 预构建镜像完全同构。
#      不使用 obd(它会把版本/磁盘/内存检查全部前置,且下载远端索引不可靠)。
#   2) thirdparty      三方仓库:distribution.yaml 中 oceanbase 的 pypi/conda/apt/spack 全为 null,
#      没有可用的三方二进制仓库 → 该层不可用。
#   3) source          源码编译:上游 oceanbase/oceanbase 依赖海量第三方子模块、编译耗时以小时计,
#      不适合在功能测试容器内完成 → 该层不可用。

# ---------- 分层获取 ----------

# 层 1:上游官方二进制 —— 官方 oceanbase-ce rpm 解包直装。
oceanbase_layer_official_binary() {
    if check_observer_ready; then
        log "an OceanBase observer is already reachable on ${OB_HOST}:${OB_PORT}; reusing it"
        return 0
    fi
    install_oceanbase_rpm || return 1
    wait_observer_ready 120 || return 1
    return 0
}

# 层 2:三方仓库 —— OceanBase 未在任何三方仓库发布与请求版本一致的二进制包。
oceanbase_layer_thirdparty_binary() {
    log "no third-party binary channel is declared for oceanbase (pypi/conda/apt/spack are null)"
    return 1
}

# 层 3:源码编译 —— 上游源码编译在功能测试容器内不可行(依赖与耗时远超用例预算)。
oceanbase_layer_source() {
    log "building OceanBase from upstream source is not feasible inside the functional container"
    return 1
}

# 分层调度器:依次尝试各层,任一层取得且版本匹配即成功;全部失败才返回非零。
acquire_oceanbase_build() {
    local layer actual
    for layer in official-binary thirdparty-binary source; do
        log "build: trying acquisition layer '${layer}' for OceanBase ${SOFTWARE_VERSION}"
        if "oceanbase_layer_${layer//-/_}"; then
            actual="$(query_observer_version)"
            if version_matches "${actual}" "${SOFTWARE_VERSION}"; then
                log "build: OceanBase ${SOFTWARE_VERSION} obtained from layer '${layer}'"
                return 0
            fi
            log "build: layer '${layer}' produced ${actual:-unknown}, expected ${SOFTWARE_VERSION}; falling back"
        else
            log "build: layer '${layer}' unavailable; falling back"
        fi
    done
    log "ERROR: every acquisition layer failed for OceanBase ${SOFTWARE_VERSION}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    install_oceanbase_dependencies || return $?

    local actual=""
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像内已解包好 observer 与启动脚本:启动镜像自带实例并复用。
        start_image_observer || return $?
        wait_observer_ready 120 || return $?
    else
        # 分层优先获取(build 模式):官方 rpm → 三方仓库 → 源码编译。
        acquire_oceanbase_build || return $?
    fi

    actual="$(query_observer_version)"
    version_matches "${actual}" "${SOFTWARE_VERSION}" || {
        log "ERROR: observer reports ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}"
    log "oceanbase ${actual} observer ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare OceanBase for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    OceanBase version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_build_standalone() {
    local status=0
    initialize_runtime || return $?
    func_prepare || return $?
    func_install || status=$?
    return "${status}"
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
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    run_build_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi