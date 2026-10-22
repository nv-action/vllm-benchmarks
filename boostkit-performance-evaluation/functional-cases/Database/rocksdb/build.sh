#!/usr/bin/env bash
# RocksDB 构建脚本:环境采集 + 准备被测软件(分层获取构建或校验)。
#   FUNC_MODE=prebuilt - 镜像已装好 RocksDB,定位 db_bench/头文件/库并校验版本
#   FUNC_MODE=build    - 分层优先获取:上游无官方二进制 → 三方仓库包 → 源码编译
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------
#
# build 模式采用“分层优先获取”:每层取得产物后必须校验实际版本 == SOFTWARE_VERSION
# (以 rocksdb/version.h 为准),不匹配或获取失败则清理该层残留并降级到下一层,三层都
# 不可行才让 install 失败。分层顺序:
#   1) upstream-binary 上游官方二进制:facebook/rocksdb 的 GitHub Release 只发布源码 tarball,
#      不提供官方预编译产物(config/distribution.yaml 记 arm64_binary=na)→ 该层不可用。
#   2) repo-binary     三方仓库(conda-forge/rocksdb、发行版 librocksdb)的二进制包。这类包通常
#      只提供库与头文件,不随包提供上游 db_bench;而本用例的 upstream-test 判定依赖 db_bench,
#      故该层只有在同时给出“版本一致的 version.h”与 db_bench 可执行文件时才可用,否则降级。
#      (distribution.yaml 的 docker 渠道为 null,不涉及镜像分发。)
#   3) source          从上游源码 tag 克隆并编译 db_bench 与静态库 —— 唯一能产出上游 db_bench 的层。

write_actual_version() {
    local actual="$1"
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}"
}

# 通用降级工具:清理某一层可能留下的构建/解包残留。
rocksdb_reset_source_tree() {
    rm -rf -- "${SOURCE_DIR}"
}

# ---------- 分层获取 ----------

# 层 1:上游官方二进制。上游 Release 仅发布源码,没有可直接使用的预编译产物。
rocksdb_layer_upstream_binary() {
    log "no official prebuilt RocksDB binary is published; upstream releases are source-only"
    return 1
}

# 层 2:三方仓库中与请求版本一致的二进制包(本用例取 conda-forge/rocksdb)。
# 必须同时提供版本一致的 version.h 与上游 db_bench,否则该层不可用,自动降级。
rocksdb_layer_repo_binary() {
    local manager prefix actual
    rocksdb_reset_source_tree
    if command -v micromamba >/dev/null 2>&1; then
        manager="micromamba"
    elif command -v conda >/dev/null 2>&1; then
        manager="conda"
    else
        log "conda/micromamba unavailable; no usable third-party binary channel for rocksdb"
        return 1
    fi
    prefix="${PERF_WORK_DIR}/rocksdb-conda"
    rm -rf -- "${prefix}"
    # conda-forge 需存在与请求版本严格一致的 rocksdb 包。
    "${manager}" create -y -p "${prefix}" -c conda-forge "rocksdb=${SOFTWARE_VERSION#v}" >/dev/null 2>&1 || {
        log "conda-forge has no rocksdb=${SOFTWARE_VERSION#v} package"
        return 1
    }
    mkdir -p "${SOURCE_DIR}"
    cp -a "${prefix}/include" "${SOURCE_DIR}/include" 2>/dev/null || true
    [[ -d "${prefix}/lib" ]] && cp -a "${prefix}/lib" "${SOURCE_DIR}/lib" 2>/dev/null || true
    [[ -x "${prefix}/bin/db_bench" ]] && cp -a "${prefix}/bin/db_bench" "${SOURCE_DIR}/db_bench"

    # 该层缺少上游 db_bench 即无法满足 upstream-test 判定,降级到源码编译。
    [[ -x "${SOURCE_DIR}/db_bench" ]] || {
        log "third-party package ships no db_bench; layer unusable for this case"
        rocksdb_reset_source_tree
        return 1
    }
    actual="$(rocksdb_version_from_header "${SOURCE_DIR}/include/rocksdb/version.h" || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION#v}" ]] || {
        log "third-party package is ${actual:-unknown}, expected ${SOFTWARE_VERSION#v}"
        rocksdb_reset_source_tree
        return 1
    }
    return 0
}

# 层 3:从上游源码 tag 克隆并编译 db_bench(会一并产出静态库),最后手段。
rocksdb_layer_source() {
    local source_tag actual
    rocksdb_reset_source_tree
    require_build_commands || return 1
    source_tag="v${SOFTWARE_VERSION#v}"
    log "cloning rocksdb ${source_tag} from ${ROCKSDB_REPOSITORY}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "${source_tag}" --depth 1 \
        "${ROCKSDB_REPOSITORY}" "${SOURCE_DIR}" || {
        log "failed to clone rocksdb ${source_tag}"
        rocksdb_reset_source_tree
        return 1
    }
    log "building rocksdb db_bench"
    # 老版本 rocksdb(如 7.9.2)靠标准库头文件的间接包含取得 uint64_t,而 GCC 13 起不再
    # 隐式提供,ubuntu 26.04 的 GCC 会让它整片报 "'uint64_t' has not been declared";
    # 用 EXTRA_CXXFLAGS 预包含 <cstdint>(Makefile 里是 CXXFLAGS += ${EXTRA_CXXFLAGS})。
    (cd "${SOURCE_DIR}" && export PORTABLE=1 DEBUG_LEVEL=0 && \
        make db_bench -j"$(nproc)" DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1 \
            EXTRA_CXXFLAGS="-include cstdint") || {
        log "rocksdb build failed"
        rocksdb_reset_source_tree
        return 1
    }
    [[ -x "${ROCKSDB_DB_BENCH_BIN}" ]] || {
        log "db_bench was not produced under ${SOURCE_DIR}"
        rocksdb_reset_source_tree
        return 1
    }
    actual="$(rocksdb_version_from_header "${ROCKSDB_INCLUDE_DIR}/rocksdb/version.h" || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION#v}" ]] || {
        log "source build produced ${actual:-unknown}, expected ${SOFTWARE_VERSION#v}"
        rocksdb_reset_source_tree
        return 1
    }
    return 0
}

# 分层调度器:依次尝试各层,任一层取得且版本匹配即成功;全部失败才返回非零。
acquire_rocksdb_build() {
    local layer
    for layer in upstream-binary repo-binary source; do
        log "build: trying acquisition layer '${layer}' for RocksDB ${SOFTWARE_VERSION}"
        if "rocksdb_layer_${layer//-/_}"; then
            log "build: RocksDB ${SOFTWARE_VERSION} obtained from layer '${layer}'"
            return 0
        fi
        log "build: layer '${layer}' unavailable or version mismatch; falling back"
    done
    log "ERROR: every acquisition layer failed for RocksDB ${SOFTWARE_VERSION}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual=""
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位 db_bench/头文件并校验版本。
        [[ -n "${ROCKSDB_DB_BENCH_BIN}" && -x "${ROCKSDB_DB_BENCH_BIN}" ]] || {
            log "ERROR: db_bench is not available in the software image"
            return 40
        }
        [[ -n "${ROCKSDB_INCLUDE_DIR}" ]] || {
            log "ERROR: rocksdb/version.h is not available in the software image"
            return 40
        }
        actual="$(rocksdb_version_from_header "${ROCKSDB_INCLUDE_DIR}/rocksdb/version.h" || true)"
    else
        # 分层优先获取(build 模式):上游无二进制 → 三方仓库包 → 源码编译。
        [[ ! -e "${SOURCE_DIR}" ]] || {
            log "ERROR: source directory is not clean: ${SOURCE_DIR}"
            return 20
        }
        acquire_rocksdb_build || return $?
        actual="$(rocksdb_version_from_header "${ROCKSDB_INCLUDE_DIR}/rocksdb/version.h" || true)"
    fi

    [[ "${actual}" == "${SOFTWARE_VERSION#v}" ]] || {
        log "ERROR: rocksdb ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    write_actual_version "${actual}" || return 40
    log "rocksdb ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare RocksDB for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    RocksDB version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_build_standalone() {
    local status=0
    initialize_runtime || return $?
    func_prepare || return $?
    func_install || status=$?
    return "${status}"
}

main() {
    parse_common_args "$@"
    case "$?" in
        0) ;;
        64)
            usage
            return 0
            ;;
        *) return 10 ;;
    esac
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    run_build_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi