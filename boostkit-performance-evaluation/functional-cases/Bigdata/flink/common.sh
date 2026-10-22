#!/usr/bin/env bash
# Apache Flink 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="flink"
CATEGORY="Bigdata"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2.3.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# Flink 是 JVM 应用,官方 tarball 与架构无关(统一 scala_2.12 发行版)。
FLINK_RELEASE_URL="${FLINK_RELEASE_URL:-https://archive.apache.org/dist/flink}"
# 层 1 下载镜像源(空格分隔,按序尝试,接入 PERF_PROXY);
# FLINK_RELEASE_URL 指向的 archive.apache.org 持有全部历史版本,始终作为最终兜底。
FLINK_MIRROR_URLS="${FLINK_MIRROR_URLS:-https://mirrors.huaweicloud.com/apache/flink https://downloads.apache.org/flink}"
FLINK_OFFLINE_DIR="${FLINK_OFFLINE_DIR:-/home/runner/software/flink}"
# build 模式分层获取(build.sh)用到的三方/源码渠道:
#   层2 = 三方仓库打包产物(PyPI apache-flink);层3 = 上游源码(最后手段)。
FLINK_PYPI_PACKAGE="${FLINK_PYPI_PACKAGE:-apache-flink}"
FLINK_SOURCE_URL="${FLINK_SOURCE_URL:-https://github.com/apache/flink.git}"
# 预构建镜像若把 Flink 装在非标准位置,可用 FLINK_HOME_OVERRIDE 显式指定。
FLINK_HOME_OVERRIDE="${FLINK_HOME_OVERRIDE:-}"
FLINK_PARALLELISM="${FLINK_PARALLELISM:-1}"

FLINK_HOME=""
FLINK_RUNTIME_DIR=""
FLINK_REST_PORT=""
FLINK_RPC_PORT=""
FLINK_BLOB_PORT=""
FLINK_TASKMANAGER_RPC_PORT=""
FLINK_REST_URL=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[flink-func] %s\n' "$*"; }

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

# 定位 Flink 安装根目录(必须含 bin/flink)。prebuilt 模式优先使用镜像内已有安装。
resolve_flink_home() {
    local candidate bin
    local -a candidates=(
        "${FLINK_HOME_OVERRIDE:-}"
        "/opt/flink"
        "/opt/flink-${SOFTWARE_VERSION}"
        "/usr/local/flink"
        "/usr/lib/flink"
        "${HOME:-/root}/flink"
    )
    bin="$(command -v flink 2>/dev/null || true)"
    if [[ -n "${bin}" ]]; then
        bin="$(cd "$(dirname "${bin}")/.." 2>/dev/null && pwd || true)"
        [[ -z "${bin}" ]] || candidates=("${bin}" "${candidates[@]}")
    fi
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -x "${candidate}/bin/flink" ]]; then
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

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内的 Flink 安装(找不到时留空,由 install 阶段报错)。
        FLINK_HOME="$(resolve_flink_home || true)"
    else
        FLINK_HOME="${PERF_WORK_DIR}/flink"
    fi
    FLINK_RUNTIME_DIR="${PERF_WORK_DIR}/runtime"

    # 用 run_id 派生一组互不冲突的本地端口,避免并行 cell 抢占 Flink 默认端口。
    local index character_code port_offset=0
    for ((index = 0; index < ${#PERF_RUN_ID}; index++)); do
        printf -v character_code '%d' "'${PERF_RUN_ID:index:1}"
        port_offset=$(((port_offset + character_code) % 10000))
    done
    FLINK_REST_PORT="$((20000 + port_offset))"
    FLINK_RPC_PORT="$((30000 + port_offset))"
    FLINK_BLOB_PORT="$((40000 + port_offset))"
    FLINK_TASKMANAGER_RPC_PORT="$((50000 + port_offset))"
    FLINK_REST_URL="http://127.0.0.1:${FLINK_REST_PORT}"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export FLINK_HOME FLINK_RUNTIME_DIR FLINK_REST_PORT FLINK_RPC_PORT FLINK_BLOB_PORT
    export FLINK_TASKMANAGER_RPC_PORT FLINK_REST_URL FLINK_PARALLELISM
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${FLINK_RUNTIME_DIR}"
}

# ---------- 下载 ----------

# 统一下载入口:curl 接入 PERF_PROXY(容器内除 dnf/apt 外的下载同样需要走代理)。
download_file() {
    local url="$1" dest="$2"
    local -a opts=(--fail --silent --show-error --location --retry 3 --connect-timeout 30 -o "${dest}")
    [[ -z "${PERF_PROXY:-}" ]] || opts+=(-x "${PERF_PROXY}")
    curl "${opts[@]}" "${url}"
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

# Flink 运行时要求的 Java 大版本(官方支持矩阵,见 1.18 release notes 与
# java_compatibility 文档:Java 17 自 1.18 起才支持,2.x 默认 17):
#   <1.18  -> 11(1.16/1.17 在 Java 17 上 JobManager 启动即崩)
#   >=1.18 -> 17
flink_java_major() {
    local major minor
    major="${SOFTWARE_VERSION%%.*}"
    minor="${SOFTWARE_VERSION#*.}"; minor="${minor%%.*}"
    if [[ "${major}" -eq 1 && "${minor}" -lt 18 ]]; then
        printf '11\n'
    else
        printf '17\n'
    fi
}

install_os_packages() {
    local logical family
    local -a rhel=() debian=()
    for logical in "$@"; do
        case "${logical}" in
            python3) rhel+=(python3); debian+=(python3) ;;
            download-tools) rhel+=(curl tar gzip); debian+=(curl tar gzip) ;;
            jdk)
                # JDK 包跟随被测 Flink 版本,不能无脑装 17(1.16/1.17 不支持 Java 17 运行时)。
                case "$(flink_java_major)" in
                    11) rhel+=(java-11-openjdk-devel); debian+=(openjdk-11-jdk-headless) ;;
                    *) rhel+=(java-17-openjdk-devel); debian+=(openjdk-17-jdk-headless) ;;
                esac
                ;;
            git) rhel+=(git); debian+=(git) ;;
            maven) rhel+=(maven); debian+=(maven) ;;
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

# build 模式所需工具:下载 + 解压 + JDK(Flink 运行/提交 job 需要 java)。
require_build_commands() {
    local -a logical=()
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        logical+=(download-tools)
    fi
    if ! command -v tar >/dev/null 2>&1; then
        logical+=(download-tools)
    fi
    if ! command -v python3 >/dev/null 2>&1; then
        logical+=(python3)
    fi
    if ! command -v java >/dev/null 2>&1; then
        logical+=(jdk)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# prebuilt 模式跑 REST smoke 需要 python3。
require_test_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing test dependencies: ${logical[*]}"
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