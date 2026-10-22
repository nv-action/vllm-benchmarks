#!/usr/bin/env bash
# protobuf 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="protobuf"
CATEGORY="HPC"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-33.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

PROTOBUF_SOURCE_URL="${PROTOBUF_SOURCE_URL:-https://github.com/protocolbuffers/protobuf.git}"
CXX_BINARY="${CXX:-g++}"
# build 模式分层获取所需:官方 release 资产地址、runner 离线目录、可选 GitHub 下载代理。
PROTOBUF_RELEASE_BASE_URL="${PROTOBUF_RELEASE_BASE_URL:-https://github.com/protocolbuffers/protobuf/releases/download}"
PROTOBUF_OFFLINE_DIR="${PROTOBUF_OFFLINE_DIR:-/home/runner/software/protobuf}"
PERF_GITHUB_DOWNLOAD_PROXY="${PERF_GITHUB_DOWNLOAD_PROXY:-}"

# 运行期路径(configure_runtime_paths 里按模式赋值)。
SOURCE_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
PROTOC_BIN=""
PROTOBUF_INC=""
PROTOBUF_CMAKE_PREFIX=""
HARNESS_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[protobuf-func] %s\n' "$*"; }

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

# prebuilt 模式:寻找含 google/protobuf/descriptor.proto 的头文件根目录。
detect_protobuf_include_root() {
    local root
    root="$(pkg-config --variable=includedir protobuf 2>/dev/null || true)"
    if [[ -n "${root}" && -f "${root}/google/protobuf/descriptor.proto" ]]; then
        printf '%s\n' "${root}"
        return 0
    fi
    local candidate
    for candidate in /usr/local/include /usr/include; do
        if [[ -f "${candidate}/google/protobuf/descriptor.proto" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
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

    # prebuilt 模式:镜像已装好 protobuf/protoc,只定位并校验版本。
    # build 模式:克隆源码,CMake 构建并安装到本次任务私有前缀。
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        PROTOC_BIN="$(command -v protoc 2>/dev/null || true)"
        SOURCE_DIR=""
        PROTOBUF_INC="$(detect_protobuf_include_root || true)"
        PROTOBUF_CMAKE_PREFIX=""
    else
        SOURCE_DIR="${PERF_WORK_DIR}/protobuf-source"
        BUILD_DIR="${PERF_WORK_DIR}/build"
        INSTALL_DIR="${PERF_WORK_DIR}/install"
        PROTOC_BIN="${INSTALL_DIR}/bin/protoc"
        PROTOBUF_INC="${SOURCE_DIR}/src"
        PROTOBUF_CMAKE_PREFIX="${INSTALL_DIR}"
    fi
    HARNESS_DIR="${PERF_WORK_DIR}/harness"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export PROTOBUF_SOURCE_URL CXX_BINARY SOURCE_DIR BUILD_DIR INSTALL_DIR PROTOC_BIN
    export PROTOBUF_INC PROTOBUF_CMAKE_PREFIX HARNESS_DIR
    export PROTOBUF_RELEASE_BASE_URL PROTOBUF_OFFLINE_DIR PERF_GITHUB_DOWNLOAD_PROXY
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# 解析 `protoc --version`(形如 "libprotoc 33.0")为版本号。
read_protoc_version() {
    local binary="$1" output
    output="$("${binary}" --version 2>&1 || true)"
    printf '%s\n' "${output}" | sed -n 's/^libprotoc[[:space:]]\+\([0-9][0-9.]*\).*/\1/p' | head -n 1
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
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(gcc g++ make) ;;
            cmake) rhel+=(cmake); debian+=(cmake) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            unzip) rhel+=(unzip); debian+=(unzip) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    (( ${#rhel[@]} == 0 )) && return 0
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

# build 模式:分层获取(官方二进制/三方包/源码编译)所需的命令。
require_build_commands() {
    local -a logical=()
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v cmake >/dev/null 2>&1 || logical+=(cmake)
    # 层1 取官方 protoc 预编译 zip 需要 curl + unzip。
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v unzip >/dev/null 2>&1 || logical+=(unzip)
    { command -v make >/dev/null 2>&1 && command -v "${CXX_BINARY}" >/dev/null 2>&1; } || logical+=(build-tools)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
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