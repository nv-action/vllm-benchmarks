#!/usr/bin/env bash
# sonic-cpp 构建脚本:环境采集 + 准备被测软件(克隆源码并构建私有副本)。
#   sonic-cpp 按提交号发布且需就地编译;两种模式都不会安装到系统。
#
# build 模式的分层优先获取分析(优先级从高到低):
#   层1 上游官方二进制:不可用。sonic-cpp 是按提交号发布的 C++ 库,官方无预编译二进制/包。
#   层2 三方仓库已打包的精确版本包:不可用。无 conda/pypi/apt/spack 渠道,且版本号是提交哈希,
#         三方仓库无法提供该精确提交对应的构建产物。
#   层3 源码编译:唯一可行层 —— 克隆仓库并 checkout 目标提交后就地 CMake 构建。
#   每层产物都需校验版本 == SOFTWARE_VERSION;此处仅层3 可用,故 build 模式直接源码编译。
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

prepare_sonic_cpp_source() {
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 20
    }
    # 版本既支持上游发布标签(如 1.0.2 -> v1.0.2),也支持提交号(短/长哈希)。
    [[ "${SOFTWARE_VERSION}" =~ ^([0-9]+\.[0-9]+\.[0-9]+|[0-9a-f]{7,40})$ ]] || {
        log "ERROR: unsupported sonic-cpp version: ${SOFTWARE_VERSION}"
        return 10
    }
    local ref
    ref="$(sonic_cpp_git_ref "${SOFTWARE_VERSION}")"
    export GIT_TERMINAL_PROMPT=0
    log "cloning sonic-cpp from ${SONIC_CPP_SOURCE_URL}"
    git clone "${SONIC_CPP_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone sonic-cpp"
        return 30
    }
    git -C "${SOURCE_DIR}" checkout --detach "${ref}" || {
        log "ERROR: failed to check out sonic-cpp ref ${ref}"
        return 30
    }
}

# sonic-cpp 的 external.cmake 用 GIT_TAG master 拉 gflags,上游默认分支已改名为 main,
# FetchContent 浅克隆必然失败。这里预先检出固定发布版本,configure 时交给 FetchContent 直接用。
prepare_sonic_cpp_gflags() {
    local dir="${PERF_WORK_DIR}/sonic-cpp-deps/gflags"
    if [[ ! -d "${dir}/.git" ]]; then
        mkdir -p "$(dirname "${dir}")"
        log "cloning gflags ${SONIC_GFLAGS_TAG} from ${SONIC_GFLAGS_URL}"
        git clone --quiet --depth 1 --branch "${SONIC_GFLAGS_TAG}" \
            "${SONIC_GFLAGS_URL}" "${dir}" || {
            log "ERROR: failed to clone gflags ${SONIC_GFLAGS_TAG}"
            return 30
        }
    fi
}

build_sonic_cpp() {
    [[ ! -e "${BUILD_DIR}" ]] || {
        log "ERROR: build directory is not clean: ${BUILD_DIR}"
        return 20
    }
    prepare_sonic_cpp_source || return $?
    prepare_sonic_cpp_gflags || return $?
    local gflags_dir="${PERF_WORK_DIR}/sonic-cpp-deps/gflags"
    log "building sonic-cpp library and official benchmark target 'bench'"
    # 上游 tests/memcmp_test.cpp 无条件引用 AVX2 的 immintrin.h,aarch64 上没有该头文件,
    # 单元测试目标无法编译;功能测试只需要库和官方 benchmark,故关闭 unittest。
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release -DBUILD_BENCH=ON -DBUILD_UNITTEST=OFF \
        -DFETCHCONTENT_SOURCE_DIR_GFLAGS="${gflags_dir}" || {
        log "ERROR: cmake configure of sonic-cpp failed"
        return 40
    }
    cmake --build "${BUILD_DIR}" -j"$(nproc)" || {
        log "ERROR: sonic-cpp library build failed"
        return 40
    }
    cmake --build "${BUILD_DIR}" --target bench -j"$(nproc)" || {
        log "ERROR: sonic-cpp benchmark target build failed"
        return 40
    }
    [[ -x "${SONIC_BENCH}" ]] || {
        log "ERROR: official benchmark binary was not produced: ${SONIC_BENCH}"
        return 40
    }
    resolve_sonic_header >/dev/null || {
        log "ERROR: sonic-cpp headers were not found under ${SONIC_INCLUDE}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_build_commands || return $?

    local actual
    build_sonic_cpp || return $?

    actual="$(read_sonic_cpp_version "${SOURCE_DIR}")"
    [[ -n "${actual}" && "${actual:0:${#SOFTWARE_VERSION}}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: checked-out revision '${actual:-unknown}' does not match ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "sonic-cpp ${SOFTWARE_VERSION} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare sonic-cpp for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    sonic-cpp release version (e.g. 1.0.2) or commit sha (default: ${SOFTWARE_VERSION})
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