#!/usr/bin/env bash
# Folly 构建脚本:环境采集 + 准备被测软件(克隆源码并编译私有共享库)。
#   folly 没有可执行入口(纯库),两种模式都在私有工作区克隆并编译一份共享库;不安装到系统。
# build 模式的分层优先获取分析(优先级从高到低):
#   层1 上游官方二进制:不可用。folly 每周自动打 tag,但官方仅发布源码,无任何预编译二进制产物。
#   层2 三方仓库已打包的精确版本包:不可用。folly 版本号是按周发布的 tag(如 ${SOFTWARE_VERSION}),
#         conda-forge/folly 与发行版仓库均无该精确版本;apt 无此包,且基础镜像内无 conda 渠道。
#   层3 源码编译:唯一可行层 —— 克隆 v<version> tag 并就地编译私有共享库。
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

prepare_folly_source() {
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 20
    }
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream source"
        return 40
    }
    log "cloning folly v${SOFTWARE_VERSION} from ${FOLLY_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${FOLLY_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone folly v${SOFTWARE_VERSION}"
        return 30
    }
}

# fast_float 是 folly 的必需依赖;系统缺失时取官方版本到私有目录。
prepare_fast_float() {
    if printf '#include <fast_float/fast_float.h>\nint main(){return 0;}\n' \
        | g++ -x c++ -fsyntax-only - 2>/dev/null; then
        log "using system fast_float"
        return 0
    fi
    log "system fast_float missing; fetching official fast_float ${FAST_FLOAT_VERSION}"
    [[ ! -e "${FAST_FLOAT_DIR}" ]] || {
        log "ERROR: fast_float directory already exists: ${FAST_FLOAT_DIR}"
        return 20
    }
    GIT_TERMINAL_PROMPT=0 git clone --branch "${FAST_FLOAT_VERSION}" --depth 1 \
        "${FAST_FLOAT_SOURCE_URL}" "${FAST_FLOAT_DIR}" || {
        log "ERROR: failed to fetch fast_float ${FAST_FLOAT_VERSION}"
        return 30
    }
    [[ -f "${FAST_FLOAT_DIR}/include/fast_float/fast_float.h" ]] || {
        log "ERROR: fast_float headers were not fetched"
        return 40
    }
}

build_folly() {
    [[ ! -e "${BUILD_DIR}" ]] || {
        log "ERROR: build directory is not clean: ${BUILD_DIR}"
        return 20
    }
    prepare_folly_source || return $?
    prepare_fast_float || return $?

    local -a fast_float_args=()
    if [[ -d "${FAST_FLOAT_DIR}/include" ]]; then
        fast_float_args=(-DFASTFLOAT_INCLUDE_DIR="${FAST_FLOAT_DIR}/include")
    fi

    log "configuring folly shared library (tests disabled)"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_SHARED_LIBS=ON \
        -DBUILD_TESTS=OFF \
        -DUSE_CMAKE_GOOGLE_TEST_INTEGRATION=OFF \
        "${fast_float_args[@]}" || {
        log "ERROR: cmake configure of folly failed"
        return 40
    }
    log "building folly library target"
    cmake --build "${BUILD_DIR}" --target folly -j"$(nproc)" || {
        log "ERROR: folly library build failed"
        return 40
    }
    [[ -f "${FOLLY_LIB}" ]] || {
        log "ERROR: libfolly shared library was not produced: ${FOLLY_LIB}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_build_commands || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" && ! -e "${FAST_FLOAT_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    require_folly_dependencies || true
    build_folly || return $?

    local actual
    actual="$(read_folly_version "${SOURCE_DIR}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: checked-out folly tag 'v${actual:-unknown}' does not match v${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "folly ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare folly for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    folly version (default: ${SOFTWARE_VERSION})
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