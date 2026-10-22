#!/usr/bin/env bash
# openviking 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="openviking"
CATEGORY="AI"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-0.4.17.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# build 模式的取版渠道(分层优先,见 build.sh 的 fetch_openviking_* 三层):
#   上游官方二进制 = 官方 PyPI wheel openviking(官方发布渠道,等效官方 release 资产);
#   三方仓库 = conda/apt 均无(见 config/distribution.yaml channels: conda: null, apt: null),无可用三方;
#   源码编译 = 官方 git 仓库精确 tag。
# 注意:官方主推分发渠道是 GHCR 镜像(ghcr.io/volcengine/openviking),build 模式运行在普通容器内、
#   没有嵌套 docker/podman,镜像类渠道无法直接使用,故改用等效的官方 wheel。
OPENVIKING_PACKAGE_INDEX="${OPENVIKING_PACKAGE_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
OPENVIKING_SOURCE_URL="${OPENVIKING_SOURCE_URL:-https://github.com/volcengine/OpenViking.git}"
OPENVIKING_PACKAGE_SPEC="openviking==${SOFTWARE_VERSION}"

# 运行期路径(configure_runtime_paths 里按模式赋值)。
VENV_DIR=""
PYBIN=""
BUILD_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[openviking-func] %s\n' "$*"; }

normalize_arch() {
    case "${1,,}" in
        x86_64 | amd64) printf 'x86_64\n' ;;
        aarch64 | arm64) printf 'aarch64\n' ;;
        *) printf '%s\n' "${1,,}" ;;
    esac
}

os_id() {
    sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' | tr 'A-Z' 'a-z'
}

os_family() {
    case "$(os_id)" in
        ubuntu | debian) printf 'debian\n' ;;
        openeuler | centos | rhel | fedora | kylin) printf 'rhel\n' ;;
        *) printf '%s\n' "$(os_id)" ;;
    esac
}

detect_os_label() {
    local id version_id
    id="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"')"
    version_id="$(sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"')"
    if [[ -n "${id}" && -n "${version_id}" ]]; then
        printf '%s-%s\n' "${id,,}" "${version_id}"
    else
        printf 'unknown\n'
    fi
}

check_architecture() {
    local actual expected
    actual="$(normalize_arch "$(uname -m)")"
    expected="$(normalize_arch "${EXPECTED_ARCH}")"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: expected architecture ${expected}, runner is ${actual}"
        return 20
    }
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log "ERROR: unsafe PERF_RUN_ID: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-func/${SOFTWARE_NAME}/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${FUNC_OS}" ]]; then
        FUNC_OS="$(detect_os_label)"
    fi
    # prebuilt 模式:软件已装好在镜像里,直接定位镜像内 python3 校验版本,不安装;
    # build 模式:装到本次任务私有的虚拟环境,避免污染系统 Python。
    VENV_DIR="${PERF_WORK_DIR}/${SOFTWARE_NAME}-venv"
    BUILD_DIR="${PERF_WORK_DIR}/build"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        PYBIN="$(command -v python3 2>/dev/null || true)"
    else
        PYBIN="${VENV_DIR}/bin/python"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export VENV_DIR PYBIN BUILD_DIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${BUILD_DIR}"
}

# 通过 import openviking 读取版本号。
read_installed_version() {
    local python_bin="${PYBIN:-python3}" version
    version="$("${python_bin}" -c 'import openviking; print(openviking.__version__)' 2>/dev/null || true)"
    if [[ -z "${version}" ]]; then
        version="$("${python_bin}" -c 'import importlib.metadata as m; print(m.version("openviking"))' 2>/dev/null || true)"
    fi
    printf '%s\n' "${version}"
}

# ---------- build 模式:分层获取辅助 ----------
# 分层顺序:上游官方二进制 → 三方仓库已打包二进制 → 源码编译。
# 每层安装到任务私有 venv 后用 venv 解释器校验版本,严格等于请求版本才接受;
# 获取失败或版本不符即回滚该层产物并降级到下一层,三层都不可行才判安装失败。

build_env_version_ok() {
    local actual
    actual="$(read_installed_version)"
    if [[ -n "${actual}" && "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        log "version check passed: openviking ${actual} == ${SOFTWARE_VERSION}"
        return 0
    fi
    log "version check failed: got '${actual:-<none>}', expected ${SOFTWARE_VERSION}"
    return 1
}

pip_uninstall_all() {
    "${VENV_DIR}/bin/pip" uninstall -y openviking >/dev/null 2>&1 || true
}

acquire_build_layers() {
    local layer
    for layer in "$@"; do
        log "trying acquisition layer: ${layer}"
        if "${layer}" && build_env_version_ok; then
            log "acquisition succeeded via ${layer}"
            return 0
        fi
        log "layer ${layer} did not yield openviking ${SOFTWARE_VERSION}; rolling back"
        pip_uninstall_all
    done
    log "ERROR: every acquisition layer failed for openviking ${SOFTWARE_VERSION}"
    return 40
}

# ---------- OS 感知的依赖安装(build 模式用) ----------

dnf_install() {
    local -a opts=()
    [[ -z "${PERF_PROXY:-}" ]] || opts+=("--setopt=proxy=${PERF_PROXY}")
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${opts[@]}" install -y "$@"
    else
        sudo -n dnf "${opts[@]}" install -y "$@"
    fi
}

apt_install() {
    local -a opts=()
    if [[ -n "${PERF_PROXY:-}" ]]; then
        opts+=(-o "Acquire::http::Proxy=${PERF_PROXY}" -o "Acquire::https::Proxy=${PERF_PROXY}")
    fi
    if [[ "$(id -u)" -eq 0 ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" update &&
            DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y "$@"
    else
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" update &&
            sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y "$@"
    fi
}

install_os_packages() {
    local logical family
    local -a rhel=() debian=()
    for logical in "$@"; do
        case "${logical}" in
            git) rhel+=(git); debian+=(git) ;;
            python3) rhel+=(python3); debian+=(python3) ;;
            pip) rhel+=(python3-pip); debian+=(python3-pip) ;;
            venv) rhel+=(python3); debian+=(python3-venv) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(gcc g++ make) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    (( ${#debian[@]} == 0 )) && return 0
    case "${family}" in
        rhel)
            log "installing via dnf: ${rhel[*]}"
            dnf_install "${rhel[@]}"
            ;;
        debian)
            log "installing via apt: ${debian[*]}"
            apt_install "${debian[@]}"
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# build 模式创建虚拟环境所需的命令:python3、pip、venv 模块。
require_python_runtime() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m pip --version >/dev/null 2>&1; then
        logical+=(pip)
    fi
    if ! command -v python3 >/dev/null 2>&1 || ! python3 -m venv --help >/dev/null 2>&1; then
        logical+=(venv)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# 官方 openviking wheel 走清华 PyPI 镜像。
pip_index_args() {
    printf '%s\n' '--trusted-host' 'pypi.tuna.tsinghua.edu.cn' \
        '--index-url' "${OPENVIKING_PACKAGE_INDEX}"
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\r\n'
}

# ---------- 独立运行共用的参数解析 ----------

parse_common_args() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                [[ "$#" -ge 2 ]] || { log "ERROR: --version requires a value"; return 10; }
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                [[ "$#" -ge 2 ]] || { log "ERROR: --results-dir requires a value"; return 10; }
                RESULTS_DIR="$2"
                shift 2
                ;;
            --os)
                [[ "$#" -ge 2 ]] || { log "ERROR: --os requires a value"; return 10; }
                FUNC_OS="$2"
                shift 2
                ;;
            --hardware)
                [[ "$#" -ge 2 ]] || { log "ERROR: --hardware requires a value"; return 10; }
                FUNC_HARDWARE="$2"
                shift 2
                ;;
            --mode)
                [[ "$#" -ge 2 ]] || { log "ERROR: --mode requires a value"; return 10; }
                FUNC_MODE="$2"
                shift 2
                ;;
            --keep-workdir)
                STANDALONE_KEEP_WORK_DIR=1
                shift
                ;;
            -h | --help)
                return 64
                ;;
            *)
                log "ERROR: unsupported option: $1"
                return 10
                ;;
        esac
    done
}