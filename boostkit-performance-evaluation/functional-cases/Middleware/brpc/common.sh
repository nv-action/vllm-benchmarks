#!/usr/bin/env bash
# brpc 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="brpc"
CATEGORY="Middleware"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.17.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

BRPC_SOURCE_URL="${BRPC_SOURCE_URL:-https://github.com/apache/brpc.git}"
# prebuilt 模式下镜像若已内置 brpc 源码树(含 example/echo_c++ 与 RELEASE_VERSION)，可用 BRPC_SOURCE_TREE 显式指向;
# 缺省时 install 阶段会浅克隆上游源码，以便复用官方 example/echo_c++。
BRPC_SOURCE_TREE="${BRPC_SOURCE_TREE:-}"

SOURCE_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
BRPC_INCLUDE_DIR=""
BRPC_LIB_DIR=""
SERVICE_DIR=""
HARNESS_DIR=""
BRPC_ECHO_PORT=""
BRPC_ECHO_SERVER_BIN=""
BRPC_ECHO_CLIENT_BIN=""
BRPC_LINK_SMOKE_BIN=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[brpc-func] %s\n' "$*"; }

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

# 在候选目录中挑选第一个存在的目录。
pick_existing_dir() {
    local candidate
    for candidate in "$@"; do
        [[ -n "${candidate}" && -d "${candidate}" ]] && {
            printf '%s\n' "${candidate}"
            return 0
        }
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
    SERVICE_DIR="${PERF_WORK_DIR}/service"
    HARNESS_DIR="${PERF_WORK_DIR}/harness"

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好 brpc:定位头文件与静态库;源码树用于官方 example(缺失时 install 阶段浅克隆)。
        local prefix
        prefix="${BRPC_PREFIX:-}"
        if [[ -z "${prefix}" ]]; then
            prefix="$(pick_existing_dir /usr/local /opt/brpc /usr || printf '/usr/local')"
        fi
        BRPC_INCLUDE_DIR="${BRPC_INCLUDE_DIR:-$(pick_existing_dir "${prefix}/include" /usr/local/include /usr/include || printf '%s' "${prefix}/include")}"
        BRPC_LIB_DIR="${BRPC_LIB_DIR:-$(pick_existing_dir "${prefix}/lib" "${prefix}/lib64" /usr/local/lib /usr/lib || printf '%s' "${prefix}/lib")}"
        SOURCE_DIR=""
    else
        SOURCE_DIR="${PERF_WORK_DIR}/brpc-source"
        BUILD_DIR="${SOURCE_DIR}/build"
        INSTALL_DIR="${BUILD_DIR}/output"
        BRPC_INCLUDE_DIR="${INSTALL_DIR}/include"
        BRPC_LIB_DIR="${INSTALL_DIR}/lib"
    fi

    BRPC_ECHO_SERVER_BIN="${SERVICE_DIR}/echo_server"
    BRPC_ECHO_CLIENT_BIN="${SERVICE_DIR}/echo_client"
    BRPC_LINK_SMOKE_BIN="${HARNESS_DIR}/link_smoke"

    if [[ -z "${BRPC_ECHO_PORT}" ]]; then
        local checksum
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        BRPC_ECHO_PORT="$((20000 + ${checksum%% *} % 20000))"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export BRPC_SOURCE_TREE BRPC_INCLUDE_DIR BRPC_LIB_DIR BRPC_ECHO_PORT
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
            git) rhel+=(git); debian+=(git) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            python3) rhel+=(python3); debian+=(python3) ;;
            gawk) rhel+=(gawk); debian+=(gawk) ;;
            sed) rhel+=(sed); debian+=(sed) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            gzip) rhel+=(gzip); debian+=(gzip) ;;
            cmake) rhel+=(cmake); debian+=(cmake) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(build-essential) ;;
            protobuf) rhel+=(protobuf-devel protobuf-compiler); debian+=(libprotobuf-dev libprotoc-dev protobuf-compiler) ;;
            gflags) rhel+=(gflags-devel); debian+=(libgflags-dev) ;;
            leveldb) rhel+=(leveldb-devel); debian+=(libleveldb-dev) ;;
            snappy) rhel+=(snappy-devel); debian+=(libsnappy-dev) ;;
            openssl) rhel+=(openssl-devel); debian+=(libssl-dev) ;;
            zlib) rhel+=(zlib-devel); debian+=(zlib1g-dev) ;;
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

# 编译与运行官方 example / 最小 smoke 所需的命令(两种模式都需要)。
require_compile_commands() {
    local -a logical=()
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v protoc >/dev/null 2>&1 || logical+=(protobuf)
    if ! command -v cc >/dev/null 2>&1 || ! command -v c++ >/dev/null 2>&1 || \
        ! command -v make >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# build 模式额外需要 cmake 与 brpc 的开发库头文件。
require_build_commands() {
    require_compile_commands || return $?
    local -a logical=()
    command -v cmake >/dev/null 2>&1 || logical+=(cmake)
    if ! printf '#include <google/protobuf/message.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(protobuf)
    fi
    if ! printf '#include <gflags/gflags.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(gflags)
    fi
    if ! printf '#include <openssl/ssl.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(openssl)
    fi
    if ! printf '#include <leveldb/db.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(leveldb)
    fi
    # 链接行固定带 -lsnappy / -lz,缺开发包会直接 cannot find -lsnappy。
    if ! printf '#include <snappy.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(snappy)
    fi
    if ! printf '#include <zlib.h>\nint main(){return 0;}\n' \
        | c++ -x c++ -fsyntax-only - 2>/dev/null; then
        logical+=(zlib)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing build dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# ---------- brpc 版本探测 ----------

# build 模式的权威版本证据是源码树根目录的 RELEASE_VERSION 文件;
# prebuilt 模式优先读取镜像内的 RELEASE_VERSION / brpc 版本头。
detect_brpc_version() {
    local file value
    local -a candidates=()
    [[ -n "${BRPC_SOURCE_TREE:-}" ]] && candidates+=("${BRPC_SOURCE_TREE}/RELEASE_VERSION")
    [[ -n "${SOURCE_DIR:-}" ]] && candidates+=("${SOURCE_DIR}/RELEASE_VERSION")
    candidates+=(
        "/usr/local/share/brpc/RELEASE_VERSION"
        "/opt/brpc/RELEASE_VERSION"
        "/usr/src/brpc/RELEASE_VERSION"
        # 预构建镜像把源码树放在 /brpc,版本证据只在源码根目录。
        "/brpc/RELEASE_VERSION"
    )
    for file in "${candidates[@]}"; do
        if [[ -f "${file}" ]]; then
            value="$(tr -d '[:space:]' <"${file}")"
            [[ -n "${value}" ]] && {
                printf '%s\n' "${value}"
                return 0
            }
        fi
    done
    for file in \
        "${BRPC_INCLUDE_DIR:-}/brpc/version.h" \
        "/usr/local/include/brpc/version.h" \
        "/usr/include/brpc/version.h"; do
        if [[ -n "${file}" && -f "${file}" ]]; then
            value="$(sed -n 's/.*BRPC_VERSION[^0-9]*\([0-9][0-9.]*\).*/\1/p' "${file}" | head -n 1)"
            [[ -n "${value}" ]] && {
                printf '%s\n' "${value}"
                return 0
            }
        fi
    done
    return 1
}

# 定位含 example/echo_c++ 的 brpc 源码树。
resolve_brpc_source_tree() {
    local candidate
    local -a candidates=(
        "${BRPC_SOURCE_TREE:-}"
        "${SOURCE_DIR:-}"
        "/opt/brpc"
        "/usr/src/brpc"
        "/usr/local/src/brpc"
        # 预构建镜像内置的源码树。
        "/brpc"
        "${HOME:-/root}/brpc"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -d "${candidate}/example/echo_c++" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

# 收集系统内 protobuf(abseil) 的共享库绝对路径,供链接示例时显式带上。
# protobuf >= 22 链接 abseil;镜像里 libbrpc/libprotobuf 的 absl 依赖必须出现在链接行,
# 否则 GNU ld 以 "DSO missing from command line" 报错。缺失的具体组件无法枚举,
# 全量发现最稳;系统没有 absl(如 protobuf 3.21)时输出为空,行为不变。
# 纯输出,供命令替换/映射调用。
absl_dso_paths() {
    local dir lib real
    local -a dirs=("${BRPC_LIB_DIR:-}" /usr/lib64 /usr/lib /usr/local/lib64 /usr/local/lib)
    local -A seen=()
    for dir in "${dirs[@]}"; do
        [[ -n "${dir}" && -d "${dir}" ]] || continue
        for lib in "${dir}"/libabsl_*.so*; do
            [[ -e "${lib}" ]] || continue
            real="$(readlink -f "${lib}" 2>/dev/null)" || real="${lib}"
            [[ -n "${real}" && -z "${seen[${real}]:-}" ]] || continue
            seen["${real}"]=1
            printf '%s\n' "${real}"
        done
    done
    return 0
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