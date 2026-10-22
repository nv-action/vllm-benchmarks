#!/usr/bin/env bash
# x265 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="x265"
CATEGORY="Media"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-4.2}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 与参考 Dockerfile 一致，使用 GitHub Release 的源码归档。
X265_ARCHIVE_BASE_URL="${X265_ARCHIVE_BASE_URL:-https://github.com/Multicorewareinc/x265/releases/download/${SOFTWARE_VERSION}}"
# prebuilt 模式下镜像若已内置源码树(含 source/),可用 X265_SOURCE_DIR(指到 source 的父目录)覆盖。
X265_SOURCE_DIR="${X265_SOURCE_DIR:-}"

SOURCE_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
X265_BIN=""
X265_WORK_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[x265-func] %s\n' "$*"; }

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

    SOURCE_DIR="${PERF_WORK_DIR}/x265_${SOFTWARE_VERSION}"
    BUILD_DIR="${SOURCE_DIR}/x265_build"
    INSTALL_DIR="${SOURCE_DIR}/x265_install"
    X265_WORK_DIR="${PERF_WORK_DIR}/encode"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好 x265:定位可执行文件。
        local path
        path="$(command -v x265 2>/dev/null || true)"
        [[ -n "${path}" ]] || path="/usr/local/bin/x265"
        X265_BIN="${path}"
    else
        X265_BIN="${INSTALL_DIR}/bin/x265"
        # 每个工作流阶段都是新进程，安装和检查阶段都需要定位私有动态库。
        case ":${LD_LIBRARY_PATH:-}:" in
            *":${INSTALL_DIR}/lib:"*) ;;
            *) export LD_LIBRARY_PATH="${INSTALL_DIR}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" ;;
        esac
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export X265_ARCHIVE_BASE_URL X265_SOURCE_DIR X265_BIN X265_WORK_DIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# ---------- OS 感知的依赖安装 ----------

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
            sed) rhel+=(sed); debian+=(sed) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            make) rhel+=(make); debian+=(make) ;;
            cmake) rhel+=(cmake); debian+=(cmake) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            ca-certificates) rhel+=(ca-certificates); debian+=(ca-certificates) ;;
            gzip) rhel+=(gzip); debian+=(gzip) ;;
            git) rhel+=(git); debian+=(git) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            build-tools) rhel+=(gcc gcc-c++); debian+=(build-essential) ;;
            nasm) rhel+=(nasm); debian+=(nasm) ;;
            ffmpeg) rhel+=(ffmpeg); debian+=(ffmpeg) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    (( ${#rhel[@]} == 0 )) && return 0
    family="$(os_family)"
    case "${family}" in
        rhel)
            log "installing via dnf: ${rhel[*]}"
            dnf_install "${rhel[@]}" || return 30
            ;;
        debian)
            log "installing via apt: ${debian[*]}"
            apt_install "${debian[@]}" || return 30
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# 下载源码归档 / cmake 构建所需的命令。
require_build_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v cmake >/dev/null 2>&1 || logical+=(cmake)
    command -v make >/dev/null 2>&1 || logical+=(make)
    command -v curl >/dev/null 2>&1 || logical+=(curl ca-certificates)
    [[ -s /etc/ssl/certs/ca-certificates.crt || -s /etc/pki/tls/certs/ca-bundle.crt ]] || logical+=(ca-certificates)
    command -v gzip >/dev/null 2>&1 || logical+=(gzip)
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    if ! command -v cc >/dev/null 2>&1 || ! command -v c++ >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    # x86_64 的汇编优化需要 nasm。
    if [[ "$(normalize_arch "${EXPECTED_ARCH}")" == "x86_64" ]] && ! command -v nasm >/dev/null 2>&1; then
        logical+=(nasm)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# 读取 x265 版本号。`x265 --version`(打到 stderr)含:
#   x265 [info]: HEVC encoder version 4.2+...
read_x265_version() {
    local out
    [[ -x "${X265_BIN}" ]] || return 1
    out="$("${X265_BIN}" --version 2>&1 || true)"
    [[ -n "${out}" ]] || return 1
    printf '%s\n' "${out}" | \
        sed -n 's/^x265 \[info\]: HEVC encoder version \([^[:space:]]*\).*$/\1/p' | head -n 1
}

# 归一化版本号:去掉 x265 的 "+<hash>" 之类的本地后缀。
base_x265_version() {
    printf '%s\n' "${1%%+*}"
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
