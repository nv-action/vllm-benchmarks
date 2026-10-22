#!/usr/bin/env bash
# openviking 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 镜像内已装好 OpenViking,只定位并校验版本,不安装
#   FUNC_MODE=build    - 装依赖 + 分层优先获取(官方 wheel→三方仓库→源码编译)到任务私有 venv
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 官方 wheel 带 cp310-abi3 标签(稳定 ABI,可被 3.10 及更高版本解释器加载),
# 因此这里只要求解释器 >= 3.10,而不是把版本钉在某个有限白名单里
# (ubuntu 26.04 自带的是 python 3.14,更早的白名单写法会把它误判为不支持)。
supported_python_version() {
    local python_version major minor
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 30
    major="${python_version%%.*}"
    minor="${python_version#*.}"
    if [[ "${major}" -eq 3 && "${minor}" -ge 10 ]]; then
        return 0
    fi
    log "ERROR: system python3 is ${python_version}, but the official OpenViking ${SOFTWARE_VERSION} wheels require python >= 3.10"
    return 30
}

# build 模式:创建任务私有 venv,再分层优先获取 OpenViking(见 common.sh acquire_build_layers)。
install_openviking_build() {
    [[ ! -e "${VENV_DIR}" ]] || {
        log "ERROR: virtual environment is not clean: ${VENV_DIR}"
        return 20
    }
    # 不要重定向到 /dev/null:log() 写的是 stdout,重定向会把失败原因一起吞掉,
    # 让 stage 看起来像"静默退出"。
    supported_python_version || return $?
    log "creating task-private virtual environment ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" || {
        log "ERROR: failed to create the virtual environment"
        return 40
    }
    [[ -x "${VENV_DIR}/bin/pip" ]] || {
        log "ERROR: the virtual environment has no usable pip"
        return 40
    }
    acquire_build_layers \
        fetch_openviking_upstream_binary \
        fetch_openviking_third_party \
        fetch_openviking_source || return $?
}

# 层 1:上游官方二进制。
# 官方主推分发渠道是 GHCR 镜像(ghcr.io/volcengine/openviking),build 模式运行在普通容器内、
# 没有嵌套 docker/podman,镜像类渠道无法直接使用;官方同时发布等价的 PyPI wheel openviking,
# 视为官方 release 资产,安装精确版本。
fetch_openviking_upstream_binary() {
    local -a index_args=()
    mapfile -t index_args < <(pip_index_args)
    log "layer 1: installing the official wheel ${OPENVIKING_PACKAGE_SPEC}"
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" --only-binary=:all: "${OPENVIKING_PACKAGE_SPEC}"
}

# 层 2:三方仓库已打包二进制。
# config/distribution.yaml 登记 OpenViking 的 conda/apt 均为 null,docker 为官方 GHCR 镜像
# (容器内不可用),故无可用三方二进制渠道。按分层设计显式跳过,交给下一层。
fetch_openviking_third_party() {
    log "layer 2: openviking has no usable third-party binary channel (conda/apt null); skip"
    return 1
}

# 层 3:源码编译(最后手段)。
# 克隆官方仓库精确 tag v${SOFTWARE_VERSION},从源码目录安装(现场构建绑定)。
fetch_openviking_source() {
    local -a index_args=()
    mapfile -t index_args < <(pip_index_args)
    local src="${BUILD_DIR}/openviking-source"
    rm -rf -- "${src}"
    log "layer 3: cloning OpenViking v${SOFTWARE_VERSION} from ${OPENVIKING_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${OPENVIKING_SOURCE_URL}" "${src}" || {
        log "ERROR: failed to clone OpenViking v${SOFTWARE_VERSION}"
        return 30
    }
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" "${src}" || {
        log "ERROR: failed to build openviking from source"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_python_runtime || return $?

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -n "${PYBIN}" && -x "${PYBIN}" ]] || {
            log "ERROR: python3 is unavailable in the software image"
            return 40
        }
        log "using the pre-installed OpenViking from the software image"
    else
        install_openviking_build || return $?
    fi

    local actual
    actual="$(read_installed_version)"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot detect the installed openviking version"
        return 40
    }
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: openviking ${actual}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "openviking ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare OpenViking for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    OpenViking version (default: ${SOFTWARE_VERSION})
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