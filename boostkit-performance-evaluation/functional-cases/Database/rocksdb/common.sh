#!/usr/bin/env bash
# RocksDB 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
# RocksDB 是进程内嵌入式 C++ 库(无服务端),因此“安装”= 构建源码,判定 = 版本 +
# 上游 db_bench 工具 + 自编译 API 烟测。
set -euo pipefail

SOFTWARE_NAME="rocksdb"
CATEGORY="Database"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-6.26.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

ROCKSDB_REPOSITORY="${ROCKSDB_REPOSITORY:-https://github.com/facebook/rocksdb.git}"
# db_bench 只读烟测规模(远小于性能用例的上亿记录)。
ROCKSDB_BENCH_NUM="${ROCKSDB_BENCH_NUM:-200000}"

# 生命周期路径(在 configure_runtime_paths 中赋值)。
SOURCE_DIR=""
ROCKSDB_DB_BENCH_BIN=""
ROCKSDB_INCLUDE_DIR=""
ROCKSDB_LIB_DIR=""
ROCKSDB_BENCH_DATA=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[rocksdb-func] %s\n' "$*"; }

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

# 在若干候选目录中定位含 rocksdb/version.h 的 include 目录。
find_rocksdb_include_dir() {
    local candidate
    local -a candidates=(
        "${ROCKSDB_INCLUDE_DIR:-}"
        "/usr/local/include"
        "/usr/include"
        "/opt/rocksdb/include"
        "${PERF_WORK_DIR}/rocksdb-source/include"
    )
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -f "${candidate}/rocksdb/version.h" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

# 目录中是否存在 rocksdb 库文件(静态库、开发软链或带版本号的共享库)。
rocksdb_dir_has_lib() {
    local dir="$1" lib
    [[ -n "${dir}" && -d "${dir}" ]] || return 1
    for lib in "${dir}/librocksdb.a" "${dir}/librocksdb.so" "${dir}"/librocksdb.so.*; do
        [[ -f "${lib}" ]] && return 0
    done
    return 1
}

# 可能存放 librocksdb 的目录:显式指定 → db_bench 同级 lib → 镜像的 LIBRARY_PATH →
# 常规库目录 → 自定义 prefix。镜像常把库装到非标准 prefix 并用 LIBRARY_PATH 暴露给链接器,
# 此时 g++ 能链上、运行时动态加载器却找不到,所以两边都以这份列表为准。
rocksdb_candidate_lib_dirs() {
    [[ -n "${ROCKSDB_LIB_DIR:-}" ]] && printf '%s\n' "${ROCKSDB_LIB_DIR}"
    if [[ -n "${ROCKSDB_DB_BENCH_BIN}" ]]; then
        printf '%s\n' "$(dirname "${ROCKSDB_DB_BENCH_BIN}")/../lib"
        printf '%s\n' "$(dirname "${ROCKSDB_DB_BENCH_BIN}")/../lib64"
    fi
    [[ -z "${LIBRARY_PATH:-}" ]] || tr ':' '\n' <<<"${LIBRARY_PATH}"
    printf '%s\n' \
        "/usr/local/lib" "/usr/local/lib64" \
        "/usr/lib" "/usr/lib64" "/lib" "/lib64" \
        "/usr/lib/$(uname -m)-linux-gnu" \
        "/opt/rocksdb/lib" "/opt/rocksdb/lib64" \
        "${PERF_WORK_DIR}/rocksdb-source" "${PERF_WORK_DIR}/rocksdb-conda/lib"
}

# 在候选目录中定位 librocksdb 库目录;都不命中时按文件名在常见 prefix 下浅层搜索。
find_rocksdb_lib_dir() {
    local candidate
    while IFS= read -r candidate; do
        if rocksdb_dir_has_lib "${candidate}"; then
            (cd "${candidate}" && pwd)
            return 0
        fi
    done < <(rocksdb_candidate_lib_dirs)
    candidate="$(find /usr/local /usr /opt /lib /lib64 -maxdepth 4 \
        \( -name 'librocksdb.a' -o -name 'librocksdb.so' \) -print -quit 2>/dev/null || true)"
    [[ -n "${candidate}" ]] || return 1
    dirname "${candidate}"
}

# db_bench 可能依赖非标准路径下的共享库(如镜像里的 librocksdb.so.N),运行前需补进
# LD_LIBRARY_PATH,否则报 "error while loading shared libraries: librocksdb.so.N"。
rocksdb_runtime_lib_path() {
    local dir out=""
    while IFS= read -r dir; do
        [[ -n "${dir}" && -d "${dir}" ]] || continue
        case "${out}" in
            *"${dir}:") continue ;;
        esac
        out+="${dir}:"
    done < <(rocksdb_candidate_lib_dirs)
    printf '%s\n' "${out}"
}

# 定位 db_bench(上游自带基准工具)。
find_rocksdb_db_bench() {
    local name="$1" path candidate
    path="$(command -v "${name}" 2>/dev/null || true)"
    [[ -n "${path}" ]] && { printf '%s\n' "${path}"; return 0; }
    for candidate in "/usr/local/bin/${name}" "/usr/bin/${name}" "${PERF_WORK_DIR}/rocksdb-source/${name}"; do
        [[ -x "${candidate}" ]] && { printf '%s\n' "${candidate}"; return 0; }
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
    ROCKSDB_BENCH_DATA="${PERF_WORK_DIR}/data"

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位 db_bench、头文件与静态库。
        ROCKSDB_DB_BENCH_BIN="$(find_rocksdb_db_bench db_bench || true)"
        ROCKSDB_INCLUDE_DIR="$(find_rocksdb_include_dir || true)"
        ROCKSDB_LIB_DIR="$(find_rocksdb_lib_dir || true)"
    else
        # 当场构建:源码、db_bench 与库都在任务私有目录。
        SOURCE_DIR="${PERF_WORK_DIR}/rocksdb-source"
        ROCKSDB_DB_BENCH_BIN="${SOURCE_DIR}/db_bench"
        ROCKSDB_INCLUDE_DIR="${SOURCE_DIR}/include"
        ROCKSDB_LIB_DIR="${SOURCE_DIR}"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export ROCKSDB_BENCH_DATA
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# 从 version.h 解析 ROCKSDB_MAJOR.MINOR.PATCH。
rocksdb_version_from_header() {
    local header="$1" major minor patch
    [[ -f "${header}" ]] || return 1
    major="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_MAJOR[[:space:]]+([0-9]+).*/\1/p' "${header}" | head -n 1)"
    minor="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_MINOR[[:space:]]+([0-9]+).*/\1/p' "${header}" | head -n 1)"
    patch="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_PATCH[[:space:]]+([0-9]+).*/\1/p' "${header}" | head -n 1)"
    [[ -n "${major}" && -n "${minor}" && -n "${patch}" ]] || return 1
    printf '%s.%s.%s\n' "${major}" "${minor}" "${patch}"
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
            curl) rhel+=(curl); debian+=(curl) ;;
            build-tools) rhel+=(gcc gcc-c++ make); debian+=(gcc g++ make) ;;
            # db_bench 运行时依赖 gflags;缺失会直接报 "Please install gflags to run rocksdb tools"。
            rocksdb-gflags)
                rhel+=(gflags-devel)
                debian+=(libgflags-dev)
                ;;
            # aarch64 上 db_bench 会 NEEDED libatomic(128 位原子操作运行时),
            # 预构建镜像可能只装了主库,运行时报 "libatomic.so.1: cannot open shared object file"。
            rocksdb-atomic)
                rhel+=(libatomic)
                debian+=(libatomic1)
                ;;
            # 压缩库开发包:RocksDB 会自动探测,缺失也能编译(功能降级)。
            rocksdb-libs)
                rhel+=(snappy-devel lz4-devel libzstd-devel zlib-devel bzip2-devel)
                debian+=(libsnappy-dev liblz4-dev libzstd-dev zlib1g-dev libbz2-dev)
                ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    (( ${#rhel[@]} == 0 && ${#debian[@]} == 0 )) && return 0
    case "${family}" in
        rhel)
            (( ${#rhel[@]} == 0 )) && return 0
            log "installing via dnf: ${rhel[*]}"
            dnf_install "${rhel[@]}"
            ;;
        debian)
            (( ${#debian[@]} == 0 )) && return 0
            log "installing via apt: ${debian[*]}"
            apt_install "${debian[@]}"
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# 运行期依赖:gflags 必装(否则 db_bench 无法启动);压缩库开发包为可选,装不上则编译降级。
ensure_rocksdb_runtime_deps() {
    install_os_packages rocksdb-gflags || return 30
    install_os_packages rocksdb-libs || log "WARN: optional compression dev packages unavailable"
}

# 按 db_bench 的动态链接实况补齐缺失的运行时库。
# 预构建镜像可能只打了 rocksdb 主库,像 libatomic 这种间接依赖会漏,导致 db_bench 直接加载失败。
ensure_db_bench_runtime_libs() {
    local line missing
    [[ -n "${ROCKSDB_DB_BENCH_BIN}" && -x "${ROCKSDB_DB_BENCH_BIN}" ]] || return 0
    command -v ldd >/dev/null 2>&1 || return 0
    while IFS= read -r line; do
        [[ "${line}" == *"not found"* ]] || continue
        missing="${line%%[[:space:]]=>*}"
        missing="${missing//[[:space:]]/}"
        case "${missing}" in
            libatomic.so*)
                log "installing libatomic runtime required by db_bench"
                install_os_packages rocksdb-atomic || return 30
                ;;
            *)
                log "WARN: unresolved shared library for db_bench: ${missing}"
                ;;
        esac
    done < <(ldd "${ROCKSDB_DB_BENCH_BIN}" 2>/dev/null || true)
    return 0
}

require_build_commands() {
    local -a logical=()
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    if ! command -v make >/dev/null 2>&1 || ! command -v cc >/dev/null 2>&1 || \
        ! command -v c++ >/dev/null 2>&1; then
        logical+=(build-tools)
    fi
    if (( ${#logical[@]} > 0 )); then
        log "installing missing dependencies: ${logical[*]}"
        install_os_packages "${logical[@]}" || return 30
    fi
    ensure_rocksdb_runtime_deps || return $?
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