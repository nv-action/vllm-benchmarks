#!/usr/bin/env bash
# glibc 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="glibc"
CATEGORY="Toolchain"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2.43}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 上游 GNU 官方发布地址(仅源码),仅 build 模式使用。
GLIBC_SOURCE_URL="${GLIBC_SOURCE_URL:-https://ftp.gnu.org/gnu/glibc}"
# glibc 2.31 用较新的 GNU Make 会反复重执行,构建时使用私有 GNU Make 4.3。
GLIBC_MAKE_URL="${GLIBC_MAKE_URL:-https://ftp.gnu.org/gnu/make}"
GLIBC_MAKE_VERSION="4.3"
GLIBC_MAKE_SHA256="e05fdde47c5f7ca45cb697e973894ff4f5d79e13b750ed57d7b66d8defc78e19"

SRC_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
MAKE_INSTALL_DIR=""
LDCONFIG_BIN=""
GLIBC_LOADER=""
GLIBC_LIB_DIR=""
GLIBC_GETCONF=""
CC_BIN=""
# build 模式分层获取选定的 glibc 产物的路径标记:install 阶段若 layer2 采用发行版仓库包
# 内置的系统 glibc,会把动态加载器/库目录/ldconfig 写入此文件,check 阶段据此复用。
LAYERED_TOOL_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[glibc-func] %s\n' "$*"; }

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

# 定位 C 编译器(用于编译功能 smoke 程序)。
resolve_c_compiler() {
    local candidate
    for candidate in \
        "$(command -v cc 2>/dev/null || true)" \
        "$(command -v gcc 2>/dev/null || true)" \
        "/usr/bin/cc" \
        "/usr/bin/gcc"; do
        if [[ -n "${candidate}" && -x "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 0
}

# glibc 版本串归一化:去掉空白与末尾句点。
# ld.so 输出形如 "stable release version 2.43.",直接截取会带上句点。
normalize_version() {
    printf '%s' "${1:-}" | tr -d ' \t\r\n' | sed 's/\.*$//'
}

# 从动态加载器版本输出解析 glibc 版本(... release version 2.31 ...)。
loader_version_of() {
    local raw
    raw="$("$1" --version 2>&1 | sed -n 's/.*release version \([0-9][0-9.]*\).*/\1/p' | head -n 1 || true)"
    normalize_version "${raw}"
}

# 在给定前缀下查找动态加载器。只用 shell 通配,不依赖 find:
# 预构建镜像常被裁剪(findutils 可能缺失),find 缺失会让探测静默失败。
loader_under_prefix() {
    local prefix="$1" dir candidate
    local -a dirs=(
        "${prefix}"/lib/*-linux-gnu*
        "${prefix}"/usr/lib/*-linux-gnu*
        "${prefix}/lib"
        "${prefix}/lib64"
        "${prefix}/lib32"
        "${prefix}/usr/lib"
        "${prefix}/usr/lib64"
        "${prefix}"
    )
    for dir in "${dirs[@]}"; do
        [[ -d "${dir}" ]] || continue
        for candidate in "${dir}"/ld-linux-*.so.* "${dir}"/ld-*.so.*; do
            [[ -e "${candidate}" ]] || continue
            printf '%s\n' "${candidate}"
            return 0
        done
    done
    return 0
}

# 系统 glibc 的动态加载器(镜像/发行版自带的 glibc 即被测对象时)。
system_glibc_loader() {
    local dir candidate
    for dir in /lib /lib64 /usr/lib /usr/lib64; do
        [[ -d "${dir}" ]] || continue
        for candidate in "${dir}"/ld-linux-*.so.* "${dir}"/ld-*.so.*; do
            [[ -e "${candidate}" ]] || continue
            printf '%s\n' "${candidate}"
            return 0
        done
    done
    return 0
}

# 加载器所在库目录。解引用符号链接:Ubuntu arm64 的 /lib/ld-linux-aarch64.so.1
# 指向 /lib/aarch64-linux-gnu/,直接用 dirname 会得到错误的多架构库路径。
loader_lib_dir() {
    local resolved
    resolved="$(readlink -f "$1" 2>/dev/null || printf '%s' "$1")"
    dirname "${resolved}"
}

# prebuilt 模式下定位镜像内 glibc 的动态加载器(优先私有前缀)。
# 预构建镜像的安装前缀是 /usr/local/glibc(Dockerfile configure --prefix)。
resolve_prebuilt_glibc_loader() {
    local root loader version fallback=""
    local -a roots=(
        "${GLIBC_PREFIX:-}"
        "/usr/local/glibc"
        "/usr/local/glibc-${SOFTWARE_VERSION}"
        "/opt/glibc-${SOFTWARE_VERSION}"
        "/opt/glibc/${SOFTWARE_VERSION}"
        "/opt/glibc"
        "/usr/local"
        "/opt"
    )
    for root in "${roots[@]}"; do
        [[ -n "${root}" && -d "${root}" ]] || continue
        loader="$(loader_under_prefix "${root}")"
        [[ -n "${loader}" ]] || continue
        [[ -n "${fallback}" ]] || fallback="${loader}"
        version="$(loader_version_of "${loader}")"
        if [[ "${version}" == "${SOFTWARE_VERSION}" ]]; then
            printf '%s\n' "${loader}"
            return 0
        fi
    done
    if [[ -n "${fallback}" ]]; then
        printf '%s\n' "${fallback}"
    else
        # 回退:系统动态加载器(镜像的系统 glibc 即被测对象时)。
        system_glibc_loader
    fi
    return 0
}

# 读取被测 glibc 版本:build 模式优先用本次构建的 ldconfig,否则用加载器或系统 ldd。
glibc_version() {
    local raw=""
    if [[ -n "${LDCONFIG_BIN}" && -x "${LDCONFIG_BIN}" ]]; then
        raw="$("${LDCONFIG_BIN}" --version 2>/dev/null | head -n 1 || true)"
    elif [[ -n "${GLIBC_LOADER}" && -x "${GLIBC_LOADER}" ]]; then
        loader_version_of "${GLIBC_LOADER}"
        return 0
    elif command -v ldd >/dev/null 2>&1; then
        raw="$(ldd --version 2>&1 | head -n 1 || true)"
    else
        return 1
    fi
    # 版本号是首行的最后一个字段,用参数展开取词,避免依赖 awk。
    normalize_version "${raw##* }"
}

# 被测 glibc 私有前缀自带的 getconf(前缀/bin/getconf);系统 glibc 为被测对象时为空。
# 发行版 .deb 解包出来的 getconf 位于 <前缀>/usr/bin,由 GLIBC_GETCONF 显式指定。
resolve_glibc_getconf() {
    local bin_dir
    if [[ -n "${GLIBC_GETCONF}" && -x "${GLIBC_GETCONF}" ]]; then
        printf '%s\n' "${GLIBC_GETCONF}"
        return 0
    fi
    [[ -n "${GLIBC_LIB_DIR}" ]] || return 0
    bin_dir="$(dirname "${GLIBC_LIB_DIR}")/bin"
    [[ -x "${bin_dir}/getconf" ]] && printf '%s\n' "${bin_dir}/getconf"
    return 0
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
    SRC_DIR="${PERF_WORK_DIR}/glibc-src"
    BUILD_DIR="${PERF_WORK_DIR}/glibc-build"
    INSTALL_DIR="${PERF_WORK_DIR}/glibc-install"
    MAKE_INSTALL_DIR="${PERF_WORK_DIR}/make-${GLIBC_MAKE_VERSION}-install"
    CC_BIN="$(resolve_c_compiler || true)"
    LAYERED_TOOL_FILE="${PERF_WORK_DIR}/layered-tool.env"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内 glibc 的动态加载器,不构建。
        LDCONFIG_BIN=""
        GLIBC_GETCONF=""
        GLIBC_LOADER="$(resolve_prebuilt_glibc_loader || true)"
        if [[ -n "${GLIBC_LOADER}" ]]; then
            GLIBC_LIB_DIR="$(loader_lib_dir "${GLIBC_LOADER}")"
        else
            GLIBC_LIB_DIR=""
        fi
    else
        # 当场构建:安装到本次任务私有前缀,不替换系统 glibc。
        LDCONFIG_BIN="${BUILD_DIR}/elf/ldconfig"
        GLIBC_GETCONF=""
        GLIBC_LIB_DIR="${INSTALL_DIR}/lib"
        GLIBC_LOADER=""
        if [[ -d "${GLIBC_LIB_DIR}" ]]; then
            GLIBC_LOADER="$(loader_under_prefix "${INSTALL_DIR}" || true)"
        fi
        # 分层获取(install 阶段)若 layer2 采用发行版仓库包的系统 glibc,则改用其路径。
        if [[ -s "${LAYERED_TOOL_FILE}" ]]; then
            local layer_key layer_value
            while IFS='=' read -r layer_key layer_value; do
                case "${layer_key}" in
                    LDCONFIG_BIN) LDCONFIG_BIN="${layer_value}" ;;
                    GLIBC_LOADER) GLIBC_LOADER="${layer_value}" ;;
                    GLIBC_LIB_DIR) GLIBC_LIB_DIR="${layer_value}" ;;
                    GLIBC_GETCONF) GLIBC_GETCONF="${layer_value}" ;;
                esac
            done <"${LAYERED_TOOL_FILE}"
        fi
    fi
    # 私有 GNU Make 优先于系统 make。
    if [[ -x "${MAKE_INSTALL_DIR}/bin/make" && "${PATH}" != "${MAKE_INSTALL_DIR}/bin:"* ]]; then
        PATH="${MAKE_INSTALL_DIR}/bin:${PATH}"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export SRC_DIR BUILD_DIR INSTALL_DIR MAKE_INSTALL_DIR LDCONFIG_BIN GLIBC_LOADER GLIBC_LIB_DIR GLIBC_GETCONF CC_BIN
    export LAYERED_TOOL_FILE
    export PATH
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
            tar) rhel+=(tar); debian+=(tar) ;;
            xz) rhel+=(xz); debian+=(xz-utils) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            ca-certificates) rhel+=(ca-certificates); debian+=(ca-certificates) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(build-essential) ;;
            bison) rhel+=(bison); debian+=(bison) ;;
            gawk) rhel+=(gawk); debian+=(gawk) ;;
            python) rhel+=(python3); debian+=(python3) ;;
            patch) rhel+=(patch); debian+=(patch) ;;
            getconf) rhel+=(glibc-common); debian+=(libc-bin) ;;
            dpkg) debian+=(dpkg) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    # 只登记了 debian 侧的逻辑包时 rhel 数组为空,不能因此跳过安装。
    if (( ${#rhel[@]} == 0 && ${#debian[@]} == 0 )); then
        return 0
    fi
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
    local missing=0 command_name
    # glibc 2.41 起源码构建必须有 python3,缺失时 configure 直接报
    # "These critical programs are missing or too old: python"。
    for command_name in curl tar xz make sha256sum gcc awk bison patch nproc python3; do
        command -v "${command_name}" >/dev/null 2>&1 || missing=1
    done
    (( missing == 0 )) && return 0
    log "installing missing glibc build dependencies"
    install_os_packages curl tar xz coreutils ca-certificates build-tools bison gawk patch python || return 30
}

# 校验阶段需要 C 编译器来编译功能程序。
require_test_compiler() {
    command -v cc >/dev/null 2>&1 && return 0
    command -v gcc >/dev/null 2>&1 && return 0
    log "installing missing C compiler"
    install_os_packages build-tools || return 30
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