#!/usr/bin/env bash
# LZ4 构建脚本:环境采集 + 准备被测软件(构建或定位)。
#   FUNC_MODE=prebuilt - 定位镜像内 lz4 CLI,浅克隆上游源码树并软链 CLI 以复用上游测试套件
#   FUNC_MODE=build    - 分层获取:上游/三方均无所需精确版本二进制,降级为源码编译
#
# build 模式的分层优先获取分析(优先级从高到低):
#   层1 上游官方二进制:不可用。lz4 GitHub releases 只发布源码 tarball,不提供 Linux 预编译二进制。
#   层2 三方仓库已打包的精确版本包:不可用。apt/conda-forge 的 lz4 版本往往滞后于 ${SOFTWARE_VERSION},
#         无法保证严格相等;且功能判定要跑上游 tests/Makefile 套件,仍需完整源码树。
#   层3 源码编译:唯一可行层 —— 克隆 v<version> tag 并编译(根 Makefile 同时产出库与 CLI)。
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

# prebuilt 模式:镜像只提供 lz4 CLI,测试脚本需当场浅克隆上游源码仓库(拿 tests/ 与 lib/)。
# 把镜像内的 lz4 软链到树的 programs/lz4,让 tests/Makefile 默认的 $LZ4 指向被测二进制。
install_lz4_test_tree() {
    local tree="${LZ4_TEST_TREE}"
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream test suite"
        return 40
    }
    rm -rf -- "${tree}"
    mkdir -p "${tree}"
    log "cloning lz4 source (tag v${SOFTWARE_VERSION}) from ${LZ4_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${LZ4_SOURCE_URL}" "${tree}" || {
        log "ERROR: failed to clone lz4 source for ${SOFTWARE_VERSION}"
        return 30
    }
    [[ -f "${tree}/tests/Makefile" ]] || {
        log "ERROR: tests/Makefile not found in the cloned lz4 source"
        return 40
    }
    mkdir -p "${tree}/programs"
    ln -sf "${LZ4_BIN}" "${tree}/programs/lz4"
    [[ -x "${tree}/programs/lz4" ]] || {
        log "ERROR: lz4 CLI is not linkable into the test tree: ${LZ4_BIN}"
        return 40
    }
    log "lz4 test tree ready at ${tree}"
}

build_lz4() {
    local source_dir="${LZ4_SOURCE_DIR}"
    [[ ! -e "${source_dir}" ]] || {
        log "ERROR: source directory is not clean: ${source_dir}"
        return 20
    }
    log "cloning lz4 ${SOFTWARE_VERSION} from ${LZ4_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${LZ4_SOURCE_URL}" "${source_dir}" || {
        log "ERROR: failed to clone lz4 ${SOFTWARE_VERSION}"
        return 30
    }
    # 根 Makefile 默认目标同时编译库与 CLI(programs/lz4)。
    log "building lz4"
    make -C "${source_dir}" -j"$(nproc)" || {
        log "ERROR: lz4 build failed"
        return 40
    }
    [[ -x "${LZ4_BIN}" ]] || {
        log "ERROR: lz4 CLI was not produced: ${LZ4_BIN}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        [[ -x "${LZ4_BIN}" ]] || {
            log "ERROR: lz4 CLI is not present in the software image"
            return 40
        }
        require_test_commands || return $?
        install_lz4_test_tree || return $?
    else
        require_build_commands || return $?
        build_lz4 || return $?
    fi

    actual="$(read_lz4_version "${LZ4_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: lz4 ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "lz4 ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare lz4 for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    lz4 version (default: ${SOFTWARE_VERSION})
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