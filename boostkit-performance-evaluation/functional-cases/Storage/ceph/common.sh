#!/usr/bin/env bash
# Ceph 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="ceph"
CATEGORY="Storage"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-19.2.6}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# Ceph 官方提供自有 apt/yum 仓库(download.ceph.com)与容器镜像。
# 版本 19.2.x 对应 squid release 线;build 模式据此选择发行套件仓库。
CEPH_RELEASE="${CEPH_RELEASE:-squid}"
CEPH_REPO_BASE="${CEPH_REPO_BASE:-https://download.ceph.com}"
# build 模式分层获取(build.sh):层1 = 上游官方二进制 tarball(官方无,仅源码 tarball);
# 层2 = 官方/发行版仓库中版本一致的包(官方镜像 quay.io/ceph/ceph 因无嵌套 docker 不可用);
# 层3 = 上游源码 tarball 编译(最后手段)。
CEPH_TARBALL_URL="${CEPH_TARBALL_URL:-https://download.ceph.com/tarballs}"
# 预构建镜像若把 Ceph 装在非标准位置,可用 CEPH_BIN_DIR_OVERRIDE 显式指定其 bin 目录。
CEPH_BIN_DIR_OVERRIDE="${CEPH_BIN_DIR_OVERRIDE:-}"
# build 模式下由框架从预构建镜像导出的根文件系统(如 Ubuntu 26.04 无对应版本的
# 发行版 ceph 包);非空时所有 ceph 命令都通过 chroot 在该根文件系统内执行。
CEPH_ROOTFS="${PERF_IMAGE_ROOTFS:-}"

CEPH_BIN=""
CEPH_MON_BIN=""
CEPH_OSD_BIN=""
CEPH_MGR_BIN=""
SERVICE_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[ceph-func] %s\n' "$*"; }

# ceph 命令统一入口:CEPH_ROOTFS 非空时在导出的根文件系统内 chroot 执行。
ceph_exec() {
    if [[ -n "${CEPH_ROOTFS}" ]]; then
        chroot "${CEPH_ROOTFS}" "$@"
    else
        "$@"
    fi
}

# 解析 ceph 命令路径(ceph-dencoder / monmaptool 等):
# CEPH_ROOTFS 模式下返回根文件系统内的绝对路径,否则走 PATH 查找。
ceph_tool_path() {
    local name="$1"
    if [[ -n "${CEPH_ROOTFS}" ]]; then
        ceph_rootfs_tool "${name}" || return 1
        return 0
    fi
    command -v "${name}" 2>/dev/null
}

# 判断可执行文件是否可运行:CEPH_ROOTFS 模式下检查根文件系统内的副本。
ceph_bin_exists() {
    local bin="$1"
    [[ -n "${bin}" ]] || return 1
    if [[ -n "${CEPH_ROOTFS}" ]]; then
        [[ -x "${CEPH_ROOTFS}${bin}" ]]
    else
        [[ -x "${bin}" ]]
    fi
}

# 输出 ceph 版本号;CEPH_ROOTFS 模式经 chroot 执行。
ceph_version_of() {
    local bin="${1:-ceph}"
    ceph_exec "${bin}" --version 2>/dev/null | sed -n 's/ceph version \([0-9][0-9.]*\).*/\1/p' | head -n 1
}

# 导出的镜像根文件系统里 /dev 只有空目录,而 chroot 内运行的 ceph 工具可能用到
# 设备节点;容器没有 mount 权限,这里用 mknod 尽量补齐,失败不影响主流程。
prepare_rootfs_devices() {
    [[ -n "${CEPH_ROOTFS}" ]] || return 0
    mkdir -p "${CEPH_ROOTFS}/dev" "${CEPH_ROOTFS}/tmp" 2>/dev/null || true
    [[ -e "${CEPH_ROOTFS}/dev/null" ]] ||
        mknod -m 666 "${CEPH_ROOTFS}/dev/null" c 1 3 2>/dev/null || true
    [[ -e "${CEPH_ROOTFS}/dev/zero" ]] ||
        mknod -m 666 "${CEPH_ROOTFS}/dev/zero" c 1 5 2>/dev/null || true
    [[ -e "${CEPH_ROOTFS}/dev/random" ]] ||
        mknod -m 666 "${CEPH_ROOTFS}/dev/random" c 1 8 2>/dev/null || true
    [[ -e "${CEPH_ROOTFS}/dev/urandom" ]] ||
        mknod -m 666 "${CEPH_ROOTFS}/dev/urandom" c 1 9 2>/dev/null || true
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

detect_os_version() {
    sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"'
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

# 解析 Ceph 客户端与守护进程可执行文件路径(优先镜像内安装)。
# CEPH_ROOTFS 非空时返回的是根文件系统内的路径(如 /usr/bin/ceph),需配合 ceph_exec 使用。
ceph_rootfs_tool() {
    local name="$1" dir
    for dir in /usr/bin /usr/local/bin /usr/sbin /sbin /opt/ceph/bin; do
        [[ -x "${CEPH_ROOTFS}${dir}/${name}" ]] || continue
        printf '%s\n' "${dir}/${name}"
        return 0
    done
    return 1
}

resolve_ceph_binaries() {
    local name dir
    local -a dirs=("${CEPH_BIN_DIR_OVERRIDE:-}")
    if [[ -n "${CEPH_ROOTFS}" ]]; then
        CEPH_BIN="$(ceph_rootfs_tool ceph || true)"
        [[ -n "${CEPH_BIN}" ]] || return 1
        CEPH_MON_BIN="$(ceph_rootfs_tool ceph-mon || true)"
        CEPH_OSD_BIN="$(ceph_rootfs_tool ceph-osd || true)"
        CEPH_MGR_BIN="$(ceph_rootfs_tool ceph-mgr || true)"
        return 0
    fi
    CEPH_BIN="$(command -v ceph 2>/dev/null || true)"
    if [[ -n "${CEPH_BIN}" ]]; then
        dirs=("$(dirname "${CEPH_BIN}")" "${dirs[@]}")
    fi
    dirs+=("/usr/bin" "/usr/local/bin" "/opt/ceph/bin")
    for name in ceph-mon ceph-osd ceph-mgr; do
        for dir in "${dirs[@]}"; do
            [[ -n "${dir}" && -x "${dir}/${name}" ]] || continue
            case "${name}" in
                ceph-mon) CEPH_MON_BIN="${dir}/${name}" ;;
                ceph-osd) CEPH_OSD_BIN="${dir}/${name}" ;;
                ceph-mgr) CEPH_MGR_BIN="${dir}/${name}" ;;
            esac
            break
        done
    done
    [[ -n "${CEPH_BIN}" ]] || return 1
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
    SERVICE_DIR="${PERF_WORK_DIR}/ceph-smoke"

    resolve_ceph_binaries || true

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export CEPH_RELEASE CEPH_REPO_BASE CEPH_BIN CEPH_MON_BIN CEPH_OSD_BIN CEPH_MGR_BIN SERVICE_DIR
    export CEPH_TARBALL_URL CEPH_ROOTFS
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${SERVICE_DIR}"
    prepare_rootfs_devices
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
            download-tools) rhel+=(curl tar gzip); debian+=(curl tar gzip) ;;
            gnupg) rhel+=(gnupg2); debian+=(gnupg) ;;
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
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        logical+=(download-tools)
    fi
    command -v gpg >/dev/null 2>&1 || logical+=(gnupg)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

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