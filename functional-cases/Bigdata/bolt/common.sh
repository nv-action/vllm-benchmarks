#!/usr/bin/env bash
# Bolt(字节跳动 C++ 原生执行引擎)功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="bolt"
CATEGORY="Bigdata"
# 上游 bytedance/bolt 不发布任何 release / tag,没有传统语义的"版本":
# 以待测 commit 短 sha 作为 SOFTWARE_VERSION(白名单见 func_case.yaml 的 versions)。
# 因此安装脚本用 git ref / commit 做版本校验。
SOFTWARE_VERSION="${SOFTWARE_VERSION:-main}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# Bolt 为纯源码分发(无预编译二进制),依赖以 Conan + CMake 管理。
BOLT_SOURCE_URL="${BOLT_SOURCE_URL:-https://github.com/bytedance/bolt.git}"
BOLT_HOME_OVERRIDE="${BOLT_HOME_OVERRIDE:-}"
# 构建类型:release / debug。release_spark 用于 Gluten/Spark 集成场景。
BOLT_BUILD_TYPE="${BOLT_BUILD_TYPE:-release}"
# 上游自带 gtest 单测的 ctest 过滤串(-R 正则),默认跑名称含 bolt_ 的子集。
BOLT_UPSTREAM_TEST_FILTER="${BOLT_UPSTREAM_TEST_FILTER:-bolt_}"

BOLT_SOURCE_DIR=""
BOLT_BUILD_DIR=""
BOLT_HOME=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[bolt-func] %s\n' "$*"; }

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

# 定位镜像内已构建/已安装的 Bolt 根目录(prebuilt 模式)。
# Bolt 无官方预编译产物,若镜像未内置则返回非零,由 install 阶段报错。
# 本函数经命令替换调用,不得向 stdout 输出日志。
resolve_bolt_root() {
    local candidate found
    local -a candidates=(
        "${BOLT_HOME_OVERRIDE:-}"
        "/workspace/bolt"
        "/opt/bolt"
        "/usr/local/bolt"
        "/usr/lib/bolt"
        "${HOME:-/root}/bolt"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -d "${candidate}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    # 策略 1:BUILD_VERSION 标记(镜像按约定提供时最可靠)。
    # -xdev 跳过 /proc /sys 等伪文件系统;独立挂载点(如 /home /data)补搜一轮。
    found="$(find / -xdev -maxdepth 5 -type f -name BUILD_VERSION 2>/dev/null | head -n 1 || true)"
    if [[ -z "${found}" ]]; then
        found="$(find /home /root /srv /data /workspace /mnt /media -maxdepth 5 \
            -type f -name BUILD_VERSION 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -n "${found}" ]]; then
        printf '%s\n' "$(dirname "${found}")"
        return 0
    fi
    # 策略 2:上游源码树发现(镜像常为 git clone + checkout 布局,
    # 目录名 bolt 且根目录有 CMakeLists.txt)。
    found="$(find / -xdev -maxdepth 4 -type d -name bolt \
        -exec test -f '{}/CMakeLists.txt' ';' -print 2>/dev/null | head -n 1 || true)"
    if [[ -z "${found}" ]]; then
        found="$(find /home /root /srv /data /workspace /mnt /media -maxdepth 4 \
            -type d -name bolt -exec test -f '{}/CMakeLists.txt' ';' -print 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -n "${found}" ]]; then
        printf '%s\n' "${found}"
        return 0
    fi
    return 1
}

# 定位含 CTestTestfile.cmake 的构建树(build 模式用 BOLT_BUILD_DIR;
# prebuilt 模式在镜像源码树 BOLT_HOME 下自动发现,深度 4 覆盖 /workspace/bolt/_build/Release)。
# 纯输出,供命令替换调用。
find_ctest_tree() {
    local marker
    if [[ -n "${BOLT_BUILD_DIR:-}" && -f "${BOLT_BUILD_DIR}/CTestTestfile.cmake" ]]; then
        printf '%s\n' "${BOLT_BUILD_DIR}"
        return 0
    fi
    [[ -n "${BOLT_HOME:-}" && -d "${BOLT_HOME}" ]] || return 1
    marker="$(find "${BOLT_HOME}" -maxdepth 4 -type f -name CTestTestfile.cmake 2>/dev/null | head -n 1 || true)"
    [[ -n "${marker}" ]] || return 1
    printf '%s\n' "$(dirname "${marker}")"
}

# ---------- 版本标识与匹配(上游无 tag,版本基线 = git ref / commit) ----------

# 读取源码树的版本标识(可选目录参数,缺省 build 模式的 BOLT_SOURCE_DIR):
# 分支检出返回分支名;detached HEAD(按 commit 测试)返回短 commit sha
# (rev-parse --abbrev-ref 此时只会返回字面量 "HEAD")。prebuilt 模式可传
# 镜像内源码树目录以读取其 git HEAD 作为版本证据。
bolt_effective_ref() {
    local dir="${1:-${BOLT_SOURCE_DIR:-}}"
    [[ -n "${dir}" && -d "${dir}/.git" ]] || return 1
    local ref
    ref="$(git -C "${dir}" rev-parse --abbrev-ref HEAD 2>/dev/null)" || return 1
    if [[ "${ref}" != "HEAD" ]]; then
        printf '%s\n' "${ref}"
        return 0
    fi
    git -C "${dir}" rev-parse --short HEAD 2>/dev/null
}

# 读取完整 commit sha(无源码树或读取失败时返回非零)。
bolt_full_commit() {
    local dir="${1:-${BOLT_SOURCE_DIR:-}}"
    [[ -n "${dir}" && -d "${dir}/.git" ]] || return 1
    git -C "${dir}" rev-parse HEAD 2>/dev/null
}

# 请求版本与实际值匹配:精确相等,或两者均为 7~40 位 hex sha 且互为前缀
# (短 sha 与完整 sha 互认,与 git 的 sha 前缀语义一致)。
bolt_version_accepts() {
    local actual="${1:-}"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] && return 0
    [[ "${actual}" =~ ^[0-9a-fA-F]{7,40}$ && "${SOFTWARE_VERSION}" =~ ^[0-9a-fA-F]{7,40}$ ]] || return 1
    [[ "${actual}" == "${SOFTWARE_VERSION}"* || "${SOFTWARE_VERSION}" == "${actual}"* ]]
}

# 源码树的实际版本(分支名/短 sha/完整 sha 任一形态)是否匹配请求版本。
bolt_ref_matches_request() {
    local actual full
    actual="$(bolt_effective_ref 2>/dev/null || true)"
    bolt_version_accepts "${actual}" && return 0
    full="$(bolt_full_commit 2>/dev/null || true)"
    bolt_version_accepts "${full}"
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
        BOLT_HOME="$(resolve_bolt_root || true)"
    else
        BOLT_SOURCE_DIR="${PERF_WORK_DIR}/bolt"
        # 上游 Makefile 的构建树在 ${SOURCE}/_build/${BUILD_TYPE}(BUILD_BASE_DIR=_build),
        # release_with_test 固定 BUILD_TYPE=Release;此前误写 build/ 导致 ctest 找错目录。
        BOLT_BUILD_DIR="${BOLT_SOURCE_DIR}/_build/${BOLT_BUILD_TYPE^}"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export BOLT_SOURCE_URL BOLT_SOURCE_DIR BOLT_BUILD_DIR BOLT_HOME BOLT_BUILD_TYPE
    export BOLT_UPSTREAM_TEST_FILTER
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
            git) rhel+=(git); debian+=(git) ;;
            ninja) rhel+=(ninja-build); debian+=(ninja-build) ;;
            python3) rhel+=(python3 python3-pip); debian+=(python3 python3-pip) ;;
            # Bolt 需要 GCC 10+ / Clang 16+(C++17),CMake 与 make。
            build-tools) rhel+=(gcc gcc-c++ make cmake); debian+=(gcc g++ make cmake) ;;
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
    # ninja 是上游 check_basic_tools 明确要求的基础工具(CMake 生成器可用)。
    if ! command -v cmake >/dev/null 2>&1 || ! command -v make >/dev/null 2>&1 || \
        ! command -v cc >/dev/null 2>&1 || ! command -v c++ >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    command -v ninja >/dev/null 2>&1 || logical+=(ninja)
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