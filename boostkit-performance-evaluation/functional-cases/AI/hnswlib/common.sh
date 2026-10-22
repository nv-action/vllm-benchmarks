#!/usr/bin/env bash
# hnswlib 功能测试共享库:由 build.sh / test.sh source。
# hnswlib 上游的主体形态是 header-only C++ 库(仓库里的 hnswlib/*.h),Python 绑定只是附加组件;
# 因此功能测试统一按 C++ 形态验证(prebuilt 与 build 两条路径完全一致):
#   - prebuilt:直接使用预构建镜像里的 /usr/local/include/hnswlib
#   - build   :从上游官方 tarball 取头文件到任务私有前缀,不写系统路径
# 头文件没有版本宏,版本识别用"头文件集合的聚合 SHA-256 指纹"(与镜像仓库自带测试同思路)。
set -euo pipefail

SOFTWARE_NAME="hnswlib"
CATEGORY="AI"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-0.9.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# build 模式的上游取版渠道:官方 release tarball(纯头文件,与架构无关)。
HNSWLIB_ARCHIVE_BASE="${HNSWLIB_ARCHIVE_BASE:-https://github.com/nmslib/hnswlib/archive/refs/tags}"
# 层 3(兜底)的上游仓库地址。
HNSWLIB_SOURCE_URL="${HNSWLIB_SOURCE_URL:-https://github.com/nmslib/hnswlib.git}"
# 编译探针用的 C++ 编译器。
CXX="${CXX:-g++}"

# 运行期路径(configure_runtime_paths 里按模式赋值)。
HNSWLIB_PREFIX=""
HNSWLIB_INCLUDE_DIR=""
BUILD_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[hnswlib-func] %s\n' "$*"; }

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

# ---------- 版本指纹 ----------
# 单个 hnswlib.h 在 0.8.0 与 0.9.0 之间完全相同,必须对整套头文件取聚合指纹才能区分版本。
# 算法:按文件名(ASCII 序)遍历目录下所有 *.h,把"文件名 + 该文件的 sha256(hex)"依次写入
# 标准输入,最后对整个流取 sha256。新增待测版本必须在此登记指纹。
# 只依赖 coreutils 的 sha256sum 与 bash 内建:精简的基础镜像里不一定有 find/awk,
# 一旦缺失会静默算出错误的聚合值(表现为"指纹对不上任何已知版本")。
header_aggregate_sha256() {
    local dir="$1" f hash
    [[ -d "${dir}" ]] || return 0
    command -v sha256sum >/dev/null 2>&1 || return 0
    (
        # LC_ALL=C 让 glob 按字节序展开,保证与登记指纹时的文件顺序一致。
        LC_ALL=C
        for f in "${dir}"/*.h; do
            [[ -f "${f}" ]] || continue
            printf '%s' "${f##*/}"
            hash="$(sha256sum "${f}")" || return 1
            printf '%s' "${hash%% *}"
        done
    ) | sha256sum | { read -r out _; printf '%s\n' "${out}"; }
}

# 诊断:打印头文件目录的实际聚合指纹与逐文件 sha256,便于与登记值比对。
log_headers_diagnostic() {
    local dir="$1" f hash
    [[ -n "${dir}" && -d "${dir}" ]] || {
        log "diagnostic: header directory is absent: ${dir:-<none>}"
        return 0
    }
    log "diagnostic: aggregate=$(header_aggregate_sha256 "${dir}") dir=${dir}"
    for f in "${dir}"/*.h; do
        [[ -f "${f}" ]] || continue
        hash="$(sha256sum "${f}" 2>/dev/null || true)"
        log "diagnostic:   ${f##*/} ${hash%% *}"
    done
}

expected_header_sha256() {
    case "$1" in
        0.9.0)
            printf '%s\n' "cc9904ab6efe2bb1a863d27ad26f376d95295b0bd9fb0342e27e51440c4347d7"
            ;;
        0.8.0)
            printf '%s\n' "d1e59344692875bd54f8a1624eb934e560d046619ac7e7cc6f37873ef364a706"
            ;;
        *)
            return 1
            ;;
    esac
}

# 反向查表:由聚合指纹得到版本号。
version_of_header_sha256() {
    case "$1" in
        cc9904ab6efe2bb1a863d27ad26f376d95295b0bd9fb0342e27e51440c4347d7)
            printf '0.9.0\n'
            ;;
        d1e59344692875bd54f8a1624eb934e560d046619ac7e7cc6f37873ef364a706)
            printf '0.8.0\n'
            ;;
        *)
            return 1
            ;;
    esac
}

# prebuilt 模式下定位镜像里的头文件目录(镜像把 hnswlib/ 装在 /usr/local/include)。
resolve_prebuilt_include_dir() {
    local candidate
    local -a candidates=(
        "/usr/local/include/hnswlib"
        "/usr/include/hnswlib"
        "/opt/hnswlib/include/hnswlib"
        "/usr/local/hnswlib/include/hnswlib"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -f "${candidate}/hnswlib.h" ]]; then
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
    BUILD_DIR="${PERF_WORK_DIR}/build"
    HNSWLIB_PREFIX="${PERF_WORK_DIR}/hnswlib-prefix"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:头文件在镜像里,只定位不安装。
        HNSWLIB_INCLUDE_DIR="$(resolve_prebuilt_include_dir || true)"
    else
        # 当场取源码:头文件装到本次任务私有前缀。
        HNSWLIB_INCLUDE_DIR="${HNSWLIB_PREFIX}/include/hnswlib"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export HNSWLIB_ARCHIVE_BASE HNSWLIB_SOURCE_URL CXX
    export HNSWLIB_PREFIX HNSWLIB_INCLUDE_DIR BUILD_DIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${BUILD_DIR}"
}

# 读出已安装 hnswlib 的版本:由头文件集合的聚合指纹反查。
read_installed_version() {
    local dir sha
    dir="${HNSWLIB_INCLUDE_DIR:-}"
    [[ -n "${dir}" && -d "${dir}" ]] || return 0
    sha="$(header_aggregate_sha256 "${dir}")"
    [[ -n "${sha}" ]] || return 0
    version_of_header_sha256 "${sha}" || true
}

# ---------- build 模式:分层获取辅助 ----------
# 分层顺序:上游官方 tarball → 三方仓库已打包产物 → 上游 git 源码(兜底)。
# 每层落地后用聚合指纹校验版本,严格等于请求版本才接受;失败即回滚该层产物并降级。

build_env_version_ok() {
    local actual
    actual="$(read_installed_version)"
    if [[ -n "${actual}" && "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        log "version check passed: hnswlib ${actual} == ${SOFTWARE_VERSION}"
        return 0
    fi
    log "version check failed: got '${actual:-<none>}', expected ${SOFTWARE_VERSION}"
    return 1
}

acquire_build_layers() {
    local layer
    for layer in "$@"; do
        log "trying acquisition layer: ${layer}"
        if "${layer}" && build_env_version_ok; then
            log "acquisition succeeded via ${layer}"
            return 0
        fi
        log "layer ${layer} did not yield hnswlib ${SOFTWARE_VERSION}; rolling back"
        rm -rf -- "${HNSWLIB_PREFIX}"
    done
    log "ERROR: every acquisition layer failed for hnswlib ${SOFTWARE_VERSION}"
    return 40
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
            curl) rhel+=(curl); debian+=(curl) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            gxx) rhel+=(gcc-c++); debian+=(g++) ;;
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

# build 模式取源码 + 编译探针所需的命令。
require_build_commands() {
    local -a logical=()
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v "${CXX}" >/dev/null 2>&1 || logical+=(gxx)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

# 指纹识别与编译探针所需的命令:prebuilt 模式同样需要(精简镜像里可能缺 sha256sum)。
require_check_commands() {
    local -a logical=()
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v "${CXX}" >/dev/null 2>&1 || logical+=(gxx)
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
