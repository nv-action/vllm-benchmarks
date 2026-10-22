#!/usr/bin/env bash
# Redis 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="redis"
CATEGORY="Database"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-8.10.2}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

REDIS_SOURCE_URL="${REDIS_SOURCE_URL:-https://github.com/redis/redis.git}"
REDIS_UPSTREAM_TEST="${REDIS_UPSTREAM_TEST:-unit/type/string}"
REDIS_SERVICE_PORT="${REDIS_SERVICE_PORT:-}"
# 上游 Tcl 套件所需的源码树:
#   REDIS_RUN_ROOT   - 本次运行的工作根(短路径),见 configure_runtime_paths 中的说明。
#   REDIS_TEST_TREE  - 本次运行为跑套件准备的树(prebuilt=浅克隆测试脚本;build=本次克隆并编译),由 configure_runtime_paths 设置。
#   REDIS_SOURCE_DIR - 显式覆盖(例如镜像已内置源码树时)。
REDIS_RUN_ROOT="${REDIS_RUN_ROOT:-}"
REDIS_TEST_TREE="${REDIS_TEST_TREE:-}"
REDIS_SOURCE_DIR="${REDIS_SOURCE_DIR:-}"

SOURCE_DIR=""
REDIS_SERVER_BIN=""
REDIS_CLI_BIN=""
SERVICE_DIR=""
PID_FILE=""
LOG_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[redis-func] %s\n' "$*"; }

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
    if [[ -z "${REDIS_SERVICE_PORT}" ]]; then
        local checksum
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        REDIS_SERVICE_PORT="$((20000 + ${checksum%% *} % 20000))"
    fi
    [[ "${REDIS_SERVICE_PORT}" =~ ^[0-9]+$ ]] && \
        ((REDIS_SERVICE_PORT >= 1024 && REDIS_SERVICE_PORT <= 65535)) || {
        log "ERROR: invalid REDIS_SERVICE_PORT: ${REDIS_SERVICE_PORT}"
        return 10
    }

    # 上游套件把 unixsocket 建在 cwd 下的 tests/tmp/server.<pid>.<n>/socket,而 AF_UNIX 路径上限 108 字节,
    # 且套件用 [file normalize](会解析软链)。PERF_WORK_DIR 很深,路径被截断成同一前缀后
    # 所有测试服务器会 bind 到同一个 socket,报 "Address already in use"。
    # 因此把运行树放到短路径 /tmp 下(不同 run_id 互不干扰;容器模式随容器销毁,原生模式在 cleanup 删除)。
    REDIS_RUN_ROOT="${REDIS_RUN_ROOT:-/tmp/redis-func-${PERF_RUN_ID}}"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像只提供二进制;install 阶段会浅克隆测试脚本并软链这些二进制到 ${REDIS_TEST_TREE}/src。
        REDIS_SERVER_BIN="$(command -v redis-server || true)"
        REDIS_CLI_BIN="$(command -v redis-cli || true)"
        REDIS_TEST_TREE="${REDIS_RUN_ROOT}/tests"
    else
        SOURCE_DIR="${REDIS_RUN_ROOT}/source"
        REDIS_SERVER_BIN="${SOURCE_DIR}/src/redis-server"
        REDIS_CLI_BIN="${SOURCE_DIR}/src/redis-cli"
        REDIS_TEST_TREE="${SOURCE_DIR}"
        REDIS_SOURCE_DIR="${SOURCE_DIR}"
    fi
    SERVICE_DIR="${PERF_WORK_DIR}/data"
    PID_FILE="${SERVICE_DIR}/redis.pid"
    LOG_FILE="${SERVICE_DIR}/redis.log"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export REDIS_SERVICE_PORT REDIS_SOURCE_DIR REDIS_TEST_TREE REDIS_RUN_ROOT
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${REDIS_RUN_ROOT}"
}

# 定位上游 Tcl 套件所需的 redis 源码树(必须含 ./runtest)。
# 首选本次运行准备的 REDIS_TEST_TREE:prebuilt 模式由 install 阶段浅克隆测试脚本并软链二进制,
# build 模式即本次克隆并编译出的源码目录。REDIS_SOURCE_DIR 为显式覆盖(镜像已内置源码树时)。
# 找不到时返回非零,由调用方判失败。
resolve_redis_source_dir() {
    local candidate
    local -a candidates=(
        "${REDIS_TEST_TREE:-}"
        "${REDIS_SOURCE_DIR:-}"
        "/opt/redis"
        "/opt/redis-${SOFTWARE_VERSION}"
        "/usr/src/redis"
        "/usr/local/src/redis"
        "/root/redis"
        "${HOME:-/root}/redis"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -f "${candidate}/runtest" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
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
            build-tools) rhel+=(gcc gcc-c++ make tar gzip); debian+=(gcc g++ make tar gzip) ;;
            tcl) rhel+=(tcl); debian+=(tcl) ;;
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

require_build_commands() {
    local -a logical=()
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    if ! command -v make >/dev/null 2>&1 || ! command -v cc >/dev/null 2>&1 || \
        ! command -v c++ >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    # 上游 Tcl 测试套件需要 tclsh。
    if ! command -v tclsh >/dev/null 2>&1 && ! command -v tclsh8.6 >/dev/null 2>&1; then
        logical+=(tcl)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# prebuilt 模式跑上游套件所需的工具:git(浅克隆测试脚本)+ tclsh(执行 runtest)。
require_test_commands() {
    local -a logical=()
    command -v git >/dev/null 2>&1 || logical+=(git)
    if ! command -v tclsh >/dev/null 2>&1 && ! command -v tclsh8.6 >/dev/null 2>&1; then
        logical+=(tcl)
    fi
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