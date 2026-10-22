#!/usr/bin/env bash
# snappy 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、版本读取、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="snappy"
CATEGORY="HPC"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.1.7}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 参考镜像的构建来源:GitHub 官方 tag 归档(与参考 Dockerfile 的下载地址一致)。
SNAPPY_RELEASE_BASE="${SNAPPY_RELEASE_BASE:-https://github.com/google/snappy/archive/refs/tags}"

# 运行期路径(configure_runtime_paths 里按模式赋值)。
SNAPPY_ARCHIVE=""
SNAPPY_SOURCE_DIR=""
SNAPPY_BUILD_DIR=""
SNAPPY_DESTDIR=""
SNAPPY_PREFIX=""
SNAPPY_PC_FILE=""
SNAPPY_LIB_FILE=""
HARNESS_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[snappy-func] %s\n' "$*"; }

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

    # 源码归档:与参考 Dockerfile 的文件名一致,保留到 check 阶段复核 SHA-256。
    SNAPPY_ARCHIVE="${PERF_WORK_DIR}/snappy-${SOFTWARE_VERSION}.tar.gz"
    SNAPPY_SOURCE_DIR="${PERF_WORK_DIR}/snappy-${SOFTWARE_VERSION}"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像按参考 Dockerfile 交付库;snappy 经 GNUInstallDirs 安装,
        # openEuler(RedHat 布局)解析为 lib64。本用例只定位交付物,不重建被测库。
        SNAPPY_PREFIX="/usr/local"
        SNAPPY_LIBDIR="lib64"
    else
        SNAPPY_BUILD_DIR="${PERF_WORK_DIR}/build"
        SNAPPY_DESTDIR="${PERF_WORK_DIR}/install-root"
        SNAPPY_PREFIX="${SNAPPY_DESTDIR}/usr/local"
        # Ubuntu(Debian 布局)上 GNUInstallDirs 解析为 lib。
        SNAPPY_LIBDIR="lib"
    fi
    # 版本证据交付物:snappy 1.1.7 的 CMake 构建不生成 snappy.pc,
    # 实际交付的是 CMake config 中的版本文件。
    SNAPPY_VERSION_CMAKE="${SNAPPY_PREFIX}/${SNAPPY_LIBDIR}/cmake/Snappy/SnappyConfigVersion.cmake"
    # 带版本号的库文件名来自 snappy CMake 的 VERSION 属性(= PROJECT_VERSION)。
    SNAPPY_LIB_FILE="${SNAPPY_PREFIX}/${SNAPPY_LIBDIR}/libsnappy.so.${SOFTWARE_VERSION}"
    HARNESS_DIR="${PERF_WORK_DIR}/harness"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export SNAPPY_RELEASE_BASE SNAPPY_ARCHIVE SNAPPY_SOURCE_DIR SNAPPY_BUILD_DIR SNAPPY_DESTDIR
    export SNAPPY_PREFIX SNAPPY_LIBDIR SNAPPY_VERSION_CMAKE SNAPPY_LIB_FILE HARNESS_DIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# 从被测交付物 SnappyConfigVersion.cmake 读取版本(set(PACKAGE_VERSION "x.y.z"))。
# snappy 1.1.7 的 CMake 构建不生成 snappy.pc,镜像交付的版本元数据是 CMake config。
read_snappy_version() {
    local version_file="$1"
    sed -n 's/^set(PACKAGE_VERSION "\([0-9][0-9.]*\)")/\1/p' \
        "${version_file}" 2>/dev/null | head -n 1
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
            python3) rhel+=(python3); debian+=(python3) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            cmake) rhel+=(cmake); debian+=(cmake) ;;
            # C++ 编译器:prebuilt 模式仅需编译回环程序,不构建 snappy 本体。
            cxx) rhel+=(gcc-c++); debian+=(g++) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(gcc g++ make) ;;
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

# build 模式:下载归档 + cmake/make 源码编译(照参考 Dockerfile)所需的命令。
require_build_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v cmake >/dev/null 2>&1 || logical+=(cmake)
    { command -v make >/dev/null 2>&1 && command -v c++ >/dev/null 2>&1; } || logical+=(build-tools)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# prebuilt 模式:下载/校验归档(python3 + curl)+ 编译回环程序(c++)所需的命令。
# 不安装 cmake/make:被测库由镜像提供,本用例不重建。
require_test_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v c++ >/dev/null 2>&1 || logical+=(cxx)
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
