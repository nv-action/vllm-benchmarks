#!/usr/bin/env bash
# Kubernetes 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="kubernetes"
CATEGORY="Middleware"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.37.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# kubectl 官方二进制下载源(dl.k8s.io),附带同名 .sha256 校验文件。
KUBECTL_DOWNLOAD_BASE="${KUBECTL_DOWNLOAD_BASE:-https://dl.k8s.io/release}"
KUBECTL_OFFLINE_DIR="${KUBECTL_OFFLINE_DIR:-/home/runner/software/kubernetes}"
# kubectl 三方版本化 apt 仓库(pkgs.k8s.io),供 build 模式仓库层获取与请求版本严格一致的包。
KUBERNETES_APT_REPO_BASE="${KUBERNETES_APT_REPO_BASE:-https://pkgs.k8s.io}"

INSTALL_DIR=""
KUBECTL_BIN=""
KUBECTL_WORK_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[kubernetes-func] %s\n' "$*"; }

normalize_arch() {
    case "${1,,}" in
        x86_64 | amd64) printf 'x86_64\n' ;;
        aarch64 | arm64) printf 'aarch64\n' ;;
        *) printf '%s\n' "${1,,}" ;;
    esac
}

# kubectl 官方下载使用 amd64/arm64 命名。
kubectl_arch() {
    case "$(normalize_arch "$1")" in
        x86_64) printf 'amd64\n' ;;
        aarch64) printf 'arm64\n' ;;
        *) printf '%s\n' "$(normalize_arch "$1")" ;;
    esac
}

# 预构建镜像里的 kubectl 不一定在 PATH 上,可能留在源码树的构建产物目录,
# 因此先查 PATH,再按镜像内常见的安装位置逐级探测。
resolve_prebuilt_kubectl() {
    local candidate
    candidate="$(command -v kubectl 2>/dev/null || true)"
    if [[ -n "${candidate}" ]]; then
        printf '%s\n' "${candidate}"
        return 0
    fi
    for candidate in \
        /usr/local/bin/kubectl \
        /usr/bin/kubectl \
        /opt/kubernetes/bin/kubectl \
        /usr/local/kubernetes-${SOFTWARE_VERSION}/_output/bin/kubectl \
        /usr/local/kubernetes-${SOFTWARE_VERSION}/_output/local/bin/linux/*/kubectl \
        /usr/local/kubernetes-${SOFTWARE_VERSION}/_output/dockerized/bin/linux/*/kubectl \
        /usr/local/kubernetes-*/_output/bin/kubectl \
        /usr/local/kubernetes-*/server/bin/kubectl; do
        if [[ -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    # 兜底:构建产物层级不固定时按文件名查找。
    candidate="$(find /usr/local /opt -maxdepth 6 -type f -name kubectl 2>/dev/null | head -n 1 || true)"
    if [[ -n "${candidate}" ]]; then
        printf '%s\n' "${candidate}"
        return 0
    fi
    printf '/usr/local/bin/kubectl\n'
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

    INSTALL_DIR="${PERF_WORK_DIR}/kubectl-install"
    KUBECTL_WORK_DIR="${PERF_WORK_DIR}/kubectl"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        KUBECTL_BIN="$(resolve_prebuilt_kubectl)"
    else
        KUBECTL_BIN="${INSTALL_DIR}/kubectl"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export KUBECTL_DOWNLOAD_BASE KUBECTL_OFFLINE_DIR KUBERNETES_APT_REPO_BASE KUBECTL_BIN KUBECTL_WORK_DIR
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
    apt_get update && apt_get install -y "$@"
}

# 以 root/sudo 运行任意 apt-get 子命令(供分层获取的仓库层按需 update/install)。
apt_get() {
    local -a opts=()
    if [[ -n "${PERF_PROXY:-}" ]]; then
        opts+=(-o "Acquire::http::Proxy=${PERF_PROXY}" -o "Acquire::https::Proxy=${PERF_PROXY}")
    fi
    if [[ "$(id -u)" -eq 0 ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" "$@"
    else
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" "$@"
    fi
}

install_os_packages() {
    local logical family
    local -a rhel=() debian=()
    for logical in "$@"; do
        case "${logical}" in
            curl) rhel+=(curl); debian+=(curl) ;;
            python3) rhel+=(python3); debian+=(python3) ;;
            sed) rhel+=(sed); debian+=(sed) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
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

# 下载/校验 kubectl 与解析 JSON 所需的命令。
require_runtime_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v sed >/dev/null 2>&1 || logical+=(sed)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# 读取 kubectl 客户端版本(去掉前导 v,例如 1.37.0)。
read_kubectl_version() {
    local out
    [[ -x "${KUBECTL_BIN}" ]] || return 1
    out="$("${KUBECTL_BIN}" version --client -o json 2>/dev/null || true)"
    [[ -n "${out}" ]] || return 1
    printf '%s' "${out}" | python3 -c \
        'import json,sys; d=json.load(sys.stdin); print(d["clientVersion"]["gitVersion"].lstrip("v"))' 2>/dev/null
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