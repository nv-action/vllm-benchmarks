#!/usr/bin/env bash
# Zstd 构建脚本:环境采集 + 准备被测软件(构建或定位)。
#   FUNC_MODE=prebuilt - 定位镜像内 zstd CLI,浅克隆上游源码树并编译 tests/datagen 供上游套件使用
#   FUNC_MODE=build    - 分层获取:优先发行版仓库的精确版本 zstd,未命中再回退源码编译
#
# build 模式的分层优先获取分析(优先级从高到低):
#   层1 上游官方二进制:不可用。zstd GitHub releases 只发布源码 tarball,不提供 Linux 预编译二进制。
#   层2 发行版仓库已打包的精确版本包:视发行版而定。Ubuntu 26.04 仓库的 zstd 为 1.5.7
#         (源码包 libzstd 1.5.7+dfsg-3),与待测版本严格相等,可直接命中;发行版滞后时未命中,
#         自动回退层3。命中后仍保留源码树,因为上游 playTests.sh 需要 tests/datagen。
#   层3 源码编译:克隆 v<version> tag 并编译,兜底层。
#   每层产物都需校验版本 == SOFTWARE_VERSION。
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

clone_zstd_tree() {
    local tree="$1"
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream source"
        return 40
    }
    [[ ! -e "${tree}" ]] || {
        log "ERROR: source directory is not clean: ${tree}"
        return 20
    }
    log "cloning zstd source (tag v${SOFTWARE_VERSION}) from ${ZSTD_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${ZSTD_SOURCE_URL}" "${tree}" || {
        log "ERROR: failed to clone zstd source for ${SOFTWARE_VERSION}"
        return 30
    }
    [[ -f "${tree}/tests/playTests.sh" ]] || {
        log "ERROR: tests/playTests.sh not found in the cloned zstd source"
        return 40
    }
}

# playTests.sh 需要 datagen 生成测试数据;只编译这一测试工具,不安装到系统。
build_zstd_datagen() {
    local tree="$1"
    log "building tests/datagen from ${tree}"
    make -C "${tree}/tests" datagen || {
        log "ERROR: failed to build tests/datagen"
        return 40
    }
    [[ -x "${ZSTD_DATAGEN_BIN}" ]] || {
        log "ERROR: tests/datagen was not created: ${ZSTD_DATAGEN_BIN}"
        return 40
    }
}

# 层2:发行版仓库已打包的 zstd CLI。命中条件:包内 zstd 版本严格等于 SOFTWARE_VERSION。
# 未安装时先按发行版安装;版本不符视为未命中,由调用方回退源码编译层。
install_distro_zstd() {
    local candidate actual
    if ! command -v zstd >/dev/null 2>&1; then
        log "trying distro package layer: install zstd from the OS repository"
        install_os_packages zstd || {
            log "distro package layer unavailable"
            return 1
        }
        command -v zstd >/dev/null 2>&1 || {
            log "distro package layer missed: zstd CLI not found after install"
            return 1
        }
    fi
    candidate="$(command -v zstd)"
    actual="$(read_zstd_version "${candidate}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "distro package layer missed: ${candidate} reports v${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 1
    }
    ZSTD_BIN="${candidate}"
    log "distro package layer hit: ${candidate} (v${actual})"
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        [[ -x "${ZSTD_BIN}" ]] || {
            log "ERROR: zstd CLI is not present in the software image"
            return 40
        }
        require_test_commands || return $?
        clone_zstd_tree "${ZSTD_TEST_TREE}" || return $?
        build_zstd_datagen "${ZSTD_TEST_TREE}" || return $?
    else
        require_build_commands || return $?
        if install_distro_zstd; then
            # 命中发行版仓库层也要源码树:上游 playTests.sh 依赖树内的 tests/datagen。
            clone_zstd_tree "${ZSTD_TEST_TREE}" || return $?
        else
            log "falling back to source build for zstd ${SOFTWARE_VERSION}"
            clone_zstd_tree "${ZSTD_SOURCE_DIR}" || return $?
            log "building zstd"
            make -C "${ZSTD_SOURCE_DIR}" -j"$(nproc)" || {
                log "ERROR: zstd build failed"
                return 40
            }
            [[ -x "${ZSTD_SOURCE_DIR}/programs/zstd" ]] || {
                log "ERROR: zstd CLI was not produced: ${ZSTD_SOURCE_DIR}/programs/zstd"
                return 40
            }
            ZSTD_BIN="${ZSTD_SOURCE_DIR}/programs/zstd"
        fi
        build_zstd_datagen "${ZSTD_TEST_TREE}" || return $?
    fi

    actual="$(read_zstd_version "${ZSTD_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: zstd ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "zstd ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare zstd for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    zstd version (default: ${SOFTWARE_VERSION})
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