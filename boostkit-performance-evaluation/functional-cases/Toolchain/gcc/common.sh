#!/usr/bin/env bash
# gcc 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="gcc"
CATEGORY="Toolchain"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-14.4.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 上游 GNU 官方发布地址(仅源码),仅 build 模式使用。
GCC_SOURCE_BASE="${GCC_SOURCE_BASE:-https://gcc.gnu.org/pub/gcc/releases}"

SRC_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
GCC_BIN=""
GXX_BIN=""
# build 模式分层获取选定的工具路径标记:install 阶段若 layer2 采用发行版仓库包的 gcc/g++,
# 会把两者写入此文件,check 阶段据此复用同一层产物。
LAYERED_TOOL_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[gcc-func] %s\n' "$*"; }

# 解析 gcc/g++ 报告的版本号。
# 发行版打包会在括号里塞额外信息,不能直接取最后一列:
#   `gcc (GCC) 12.3.1 20230508 (openEuler 12.3.1-110.oe2403sp4)`   -> 12.3.1
#   `gcc (Ubuntu 16-20260816-1ubuntu1) 16.0.1 20260816 (experimental) [master r16-8246-g569ace1fa50]`
#                                                                   -> 16.0.1
# 因此先跳过开头的 "(...)",再取首个版本号字段。
gcc_version_of() {
    local first rest word="" token
    first="$("$1" --version 2>/dev/null | head -n 1 || true)"
    rest="${first#*)}"
    read -r word _ <<<"${rest}"
    if [[ ! "${word}" =~ ^[0-9]+\.[0-9] ]]; then
        word=""
        for token in ${rest}; do
            [[ "${token}" =~ ^[0-9]+\.[0-9] ]] || continue
            word="${token}"
            break
        done
    fi
    printf '%s\n' "${word}"
}

# prebuilt 模式下定位镜像内已装好的 gcc 或 g++:依次探测常见位置,
# 优先返回版本与请求版本一致的二进制,否则返回首个可执行文件(交给版本校验报告差异)。
resolve_prebuilt_gcc() {
    local name="$1" candidate fallback="" version
    local -a candidates=()
    case "${name}" in
        gcc)
            candidates=(
                "$(command -v gcc 2>/dev/null || true)"
                "/usr/bin/gcc"
                "/usr/local/gcc/bin/gcc"
                "/usr/local/bin/gcc"
                "/opt/gcc/bin/gcc"
                "/opt/gcc-${SOFTWARE_VERSION}/bin/gcc"
                "/usr/local/gcc-${SOFTWARE_VERSION}/bin/gcc"
            )
            ;;
        g++)
            candidates=(
                "$(command -v g++ 2>/dev/null || true)"
                "/usr/bin/g++"
                "/usr/local/gcc/bin/g++"
                "/usr/local/bin/g++"
                "/opt/gcc/bin/g++"
                "/opt/gcc-${SOFTWARE_VERSION}/bin/g++"
                "/usr/local/gcc-${SOFTWARE_VERSION}/bin/g++"
            )
            ;;
    esac
    for candidate in "${candidates[@]}"; do
        [[ -n "${candidate}" && -x "${candidate}" ]] || continue
        [[ -n "${fallback}" ]] || fallback="${candidate}"
        version="$(gcc_version_of "${candidate}")"
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
    SRC_DIR="${PERF_WORK_DIR}/gcc-src"
    BUILD_DIR="${PERF_WORK_DIR}/gcc-build"
    INSTALL_DIR="${PERF_WORK_DIR}/gcc-install"
    LAYERED_TOOL_FILE="${PERF_WORK_DIR}/layered-tool.env"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内的 gcc/g++,不构建。
        GCC_BIN="$(resolve_prebuilt_gcc gcc || true)"
        GXX_BIN="$(resolve_prebuilt_gcc g++ || true)"
    else
        # 当场构建:安装到本次任务私有前缀,不替换系统 gcc。
        GCC_BIN="${INSTALL_DIR}/bin/gcc"
        GXX_BIN="${INSTALL_DIR}/bin/g++"
        # 分层获取(install 阶段)若 layer2 采用发行版仓库包的 gcc,则改用其路径。
        if [[ -s "${LAYERED_TOOL_FILE}" ]]; then
            local layer_key layer_value
            while IFS='=' read -r layer_key layer_value; do
                case "${layer_key}" in
                    GCC_BIN) GCC_BIN="${layer_value}" ;;
                    GXX_BIN) GXX_BIN="${layer_value}" ;;
                esac
            done <"${LAYERED_TOOL_FILE}"
        fi
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export SRC_DIR BUILD_DIR INSTALL_DIR GCC_BIN GXX_BIN LAYERED_TOOL_FILE
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
            bison) rhel+=(bison); debian+=(bison) ;;
            flex) rhel+=(flex); debian+=(flex) ;;
            gmp) rhel+=(gmp-devel); debian+=(libgmp-dev) ;;
            mpfr) rhel+=(mpfr-devel); debian+=(libmpfr-dev) ;;
            mpc) rhel+=(libmpc-devel); debian+=(libmpc-dev) ;;
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
    local logical missing=0
    local -a packages=()
    for logical in curl tar xz make sha256sum bison flex; do
        if ! command -v "${logical}" >/dev/null 2>&1; then
            missing=1
        fi
    done
    # GMP/MPFR/MPC 开发头文件缺失时无法构建 GCC。
    if ! printf '#include <gmp.h>\n#include <mpfr.h>\n#include <mpc.h>\nint main(void){return 0;}\n' |
        c++ -x c++ -fsyntax-only - >/dev/null 2>&1; then
        missing=1
    fi
    (( missing == 0 )) && return 0
    log "installing missing GCC build dependencies"
    install_os_packages curl tar xz coreutils ca-certificates build-tools bison flex gmp mpfr mpc || return 30
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