#!/usr/bin/env bash
# Envoy 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="envoy"
CATEGORY="Middleware"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.39.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
PERF_GITHUB_DOWNLOAD_PROXY="${PERF_GITHUB_DOWNLOAD_PROXY:-}"

# 官方 release 资产与离线目录(优先复用 runner 离线镜像,其次按 sha256 校验的官方下载)。
ENVOY_OFFLINE_DIR="${ENVOY_OFFLINE_DIR:-/home/runner/software/envoy}"

INSTALL_DIR=""
ENVOY_BIN=""
RUNTIME_DIR=""
ENVOY_LISTENER_PORT=""
ENVOY_ADMIN_PORT=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[envoy-func] %s\n' "$*"; }

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

# 读取 envoy 版本号。`envoy --version` 形如:
#   envoy  version: <hash>/1.39.1/<build>/<ssl>...
# 1.40 的输出首行是空行,故取第一行非空内容再解析。
read_envoy_version() {
    local line value
    [[ -x "${ENVOY_BIN}" ]] || return 1
    line="$("${ENVOY_BIN}" --version 2>/dev/null | sed -n '/[^[:space:]]/{p;q;}')"
    value="$(printf '%s\n' "${line}" | sed -n 's#.*/\([0-9][0-9.]*\)/.*#\1#p' | head -n 1)"
    if [[ -z "${value}" ]]; then
        value="$(printf '%s\n' "${line}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
    fi
    printf '%s\n' "${value}"
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

    INSTALL_DIR="${PERF_WORK_DIR}/envoy-install"
    RUNTIME_DIR="${PERF_WORK_DIR}/runtime"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好 envoy:按候选路径定位可执行文件。
        # 官方预构建镜像把二进制放在 /usr/local/envoy/envoy,不一定是 PATH 上的 envoy。
        local candidate
        ENVOY_BIN=""
        for candidate in "$(command -v envoy 2>/dev/null || true)" \
            /usr/local/bin/envoy /usr/local/envoy/envoy /usr/bin/envoy; do
            if [[ -n "${candidate}" && -x "${candidate}" ]]; then
                ENVOY_BIN="${candidate}"
                break
            fi
        done
        [[ -n "${ENVOY_BIN}" ]] || ENVOY_BIN="/usr/local/bin/envoy"
    else
        ENVOY_BIN="${INSTALL_DIR}/envoy"
    fi

    if [[ -z "${ENVOY_LISTENER_PORT}" ]]; then
        local checksum
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        ENVOY_LISTENER_PORT="$((20000 + ${checksum%% *} % 10000))"
        ENVOY_ADMIN_PORT="$((ENVOY_LISTENER_PORT + 1))"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export ENVOY_OFFLINE_DIR ENVOY_BIN ENVOY_LISTENER_PORT ENVOY_ADMIN_PORT
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
            curl) rhel+=(curl); debian+=(curl) ;;
            python3) rhel+=(python3); debian+=(python3) ;;
            gawk) rhel+=(gawk); debian+=(gawk) ;;
            sed) rhel+=(sed); debian+=(sed) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            gzip) rhel+=(gzip); debian+=(gzip) ;;
            procps) rhel+=(procps-ng); debian+=(procps) ;;
            openssl) rhel+=(openssl); debian+=(openssl) ;;
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

# 下载/校验/启动 envoy 都需要的命令。
require_runtime_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v tee >/dev/null 2>&1 || logical+=(coreutils)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v gzip >/dev/null 2>&1 || logical+=(gzip)
    command -v pgrep >/dev/null 2>&1 || logical+=(procps)
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