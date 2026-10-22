#!/usr/bin/env bash
# python (CPython) 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="python"
CATEGORY="Toolchain"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-3.14.7}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 上游 CPython 官方发布地址(源码 tarball),仅 build 模式使用。
PYTHON_SOURCE_URL="${PYTHON_SOURCE_URL:-https://www.python.org/ftp/python}"
# 与性能用例一致的构建配置(开启 PGO 与 LTO)。
CONFIGURE_OPTIONS="${CONFIGURE_OPTIONS:---enable-optimizations --with-lto}"

PYTHON_SOURCE_DIR=""
PYTHON_INSTALL_DIR=""
PYTHON_BIN=""
PYTHON_MM=""
# build 模式分层获取选定的工具路径标记:install 阶段任一层替换了默认的私有前缀解释器,
# 会把 PYTHON_BIN 写入此文件,check 阶段据此复用同一层产物。
LAYERED_TOOL_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[python-func] %s\n' "$*"; }

# 解析解释器报告的版本号(Python 3.14.7 -> 3.14.7)。
python_version_of() {
    "$1" --version 2>&1 | awk '{print $2}' || true
}

# prebuilt 模式下定位镜像内已装好的 python3:依次探测常见位置,
# 优先返回版本与请求版本一致的二进制,否则返回首个可执行文件(交给版本校验报告差异)。
resolve_prebuilt_python() {
    local candidate fallback="" version
    local -a candidates=(
        "$(command -v python3 2>/dev/null || true)"
        "$(command -v "python${PYTHON_MM}" 2>/dev/null || true)"
        "/usr/local/bin/python3"
        "/usr/local/bin/python${PYTHON_MM}"
        "/usr/local/python3/bin/python3"
        "/usr/local/python${PYTHON_MM}/bin/python3"
        "/opt/python/bin/python3"
        "/opt/python-${SOFTWARE_VERSION}/bin/python3"
        "/opt/python${PYTHON_MM}/bin/python3"
    )
    for candidate in "${candidates[@]}"; do
        [[ -n "${candidate}" && -x "${candidate}" ]] || continue
        [[ -n "${fallback}" ]] || fallback="${candidate}"
        version="$(python_version_of "${candidate}")"
        if [[ "${version}" == "${SOFTWARE_VERSION}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    [[ -n "${fallback}" ]] && printf '%s\n' "${fallback}"
    return 0
}

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
    # 主次版本号(3.14.7 -> 3.14),用于定位 python3.14 之类的解释器。
    PYTHON_MM="${SOFTWARE_VERSION%.*}"
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
    PYTHON_SOURCE_DIR="${PERF_WORK_DIR}/python-source"
    PYTHON_INSTALL_DIR="${PERF_WORK_DIR}/python-install"
    LAYERED_TOOL_FILE="${PERF_WORK_DIR}/layered-tool.env"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内的 python3,不构建。
        PYTHON_BIN="$(resolve_prebuilt_python || true)"
    else
        # 当场构建:安装到本次任务私有前缀,不覆盖系统 Python。
        PYTHON_BIN="${PYTHON_INSTALL_DIR}/bin/python3"
        # 分层获取(install 阶段)若 layer2 采用发行版仓库包的 python,则改用其路径。
        if [[ -s "${LAYERED_TOOL_FILE}" ]]; then
            local layer_key layer_value
            while IFS='=' read -r layer_key layer_value; do
                if [[ "${layer_key}" == "PYTHON_BIN" ]]; then
                    PYTHON_BIN="${layer_value}"
                fi
            done <"${LAYERED_TOOL_FILE}"
        fi
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export PYTHON_SOURCE_DIR PYTHON_INSTALL_DIR PYTHON_BIN PYTHON_MM CONFIGURE_OPTIONS
    export LAYERED_TOOL_FILE
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
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
            curl) rhel+=(curl); debian+=(curl) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            xz) rhel+=(xz); debian+=(xz-utils) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            ca-certificates) rhel+=(ca-certificates); debian+=(ca-certificates) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(build-essential) ;;
            zlib-dev) rhel+=(zlib-devel); debian+=(zlib1g-dev) ;;
            openssl-dev) rhel+=(openssl-devel); debian+=(libssl-dev) ;;
            libffi-dev) rhel+=(libffi-devel); debian+=(libffi-dev) ;;
            bzip2-dev) rhel+=(bzip2-devel); debian+=(libbz2-dev) ;;
            readline-dev) rhel+=(readline-devel); debian+=(libreadline-dev) ;;
            sqlite-dev) rhel+=(sqlite-devel); debian+=(libsqlite3-dev) ;;
            uuid-dev) rhel+=(libuuid-devel); debian+=(uuid-dev) ;;
            gdbm-dev) rhel+=(gdbm-devel); debian+=(libgdbm-dev) ;;
            xz-dev) rhel+=(xz-devel); debian+=(liblzma-dev) ;;
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
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v xz >/dev/null 2>&1 || logical+=(xz)
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    if ! command -v cc >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" \
        zlib-dev openssl-dev libffi-dev bzip2-dev readline-dev \
        sqlite-dev uuid-dev gdbm-dev xz-dev || return 30
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