#!/usr/bin/env bash
# Redis 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 redis 版本,浅克隆上游测试脚本并软链镜像内二进制
#   FUNC_MODE=build    - 装依赖 + 克隆源码 + 编译
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

# 把镜像/PATH 中已有的 redis 二进制软链进测试树的 src/,
# 让上游 ./runtest 的本地模式能从树里拉起 redis-server(不依赖镜像内置源码)。
link_redis_binaries() {
    local tree="$1" name path
    local src_dir="${tree}/src"
    mkdir -p "${src_dir}"
    for name in redis-server redis-cli redis-benchmark redis-sentinel redis-check-rdb redis-check-aof; do
        path="$(command -v "${name}" 2>/dev/null || true)"
        [[ -n "${path}" ]] || continue
        ln -sf "${path}" "${src_dir}/${name}"
    done
    [[ -x "${src_dir}/redis-server" && -x "${src_dir}/redis-cli" ]] || {
        log "ERROR: redis binaries not found on PATH to link into ${src_dir}"
        return 40
    }
}

# prebuilt 模式:镜像只提供二进制,测试脚本需当场浅克隆上游源码仓库(只为拿 ./runtest 与 tests/)。
install_redis_test_tree() {
    local tree="${REDIS_TEST_TREE}"
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream test suite"
        return 40
    }
    rm -rf -- "${tree}"
    mkdir -p "${tree}"
    log "cloning redis test suite (tag ${SOFTWARE_VERSION}) from ${REDIS_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${REDIS_SOURCE_URL}" "${tree}" || {
        log "ERROR: failed to clone redis test suite for ${SOFTWARE_VERSION}"
        return 30
    }
    [[ -f "${tree}/runtest" ]] || {
        log "ERROR: runtest not found in the cloned test suite"
        return 40
    }
    link_redis_binaries "${tree}" || return $?
    log "redis test tree ready at ${tree}"
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:校验版本,并浅克隆测试脚本 + 软链镜像内二进制。
        [[ -x "${REDIS_SERVER_BIN}" ]] || {
            log "ERROR: redis-server is not present in the software image"
            return 40
        }
        [[ -x "${REDIS_CLI_BIN}" ]] || {
            log "ERROR: redis-cli is not present in the software image"
            return 40
        }
        require_test_commands || return $?
        install_redis_test_tree || return $?
    else
        # 当场构建:装依赖,克隆源码,编译。
        require_build_commands || return $?
        [[ ! -e "${SOURCE_DIR}" ]] || {
            log "ERROR: source directory is not clean: ${SOURCE_DIR}"
            return 20
        }
        log "cloning redis ${SOFTWARE_VERSION} from ${REDIS_SOURCE_URL}"
        GIT_TERMINAL_PROMPT=0 git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
            "${REDIS_SOURCE_URL}" "${SOURCE_DIR}" || {
            log "ERROR: failed to clone redis ${SOFTWARE_VERSION}"
            return 30
        }
        log "building redis"
        (cd "${SOURCE_DIR}" && make -j"$(nproc)" BUILD_TLS=no) || {
            log "ERROR: redis build failed"
            return 40
        }
        [[ -x "${REDIS_SERVER_BIN}" && -x "${REDIS_CLI_BIN}" ]] || {
            log "ERROR: expected redis binaries were not produced"
            return 40
        }
    fi

    actual="$("${REDIS_SERVER_BIN}" --version | sed -n 's/.*v=\([^ ]*\).*/\1/p' | head -n 1)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: redis ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "redis ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare redis for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Redis version (default: ${SOFTWARE_VERSION})
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