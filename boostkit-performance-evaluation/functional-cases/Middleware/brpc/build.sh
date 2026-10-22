#!/usr/bin/env bash
# brpc 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 brpc 版本,浅克隆上游源码以复用官方 example/echo_c++
#   FUNC_MODE=build    - 分层获取:上游二进制/仓库包均不可行,最终以源码编译(克隆 + cmake)为准
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# 链接 libbrpc 所需的基础依赖库;abseil 依赖由 build_link_libs 动态追加。
BRPC_BASE_LINK_LIBS=(-lbrpc -lprotobuf -lgflags -lssl -lcrypto -lleveldb -lsnappy -lz -lpthread -ldl -lrt)
BRPC_LINK_LIBS=()

# 组装最终链接行:基础依赖 + absl 共享库绝对路径(存在才追加,protobuf>=22 需要;
# 传 .so 绝对路径可解掉静态 libbrpc 成员对 absl 符号的未定义引用)。
build_link_libs() {
    BRPC_LINK_LIBS=("${BRPC_BASE_LINK_LIBS[@]}")
    local f
    while IFS= read -r f; do
        [[ -n "${f}" ]] && BRPC_LINK_LIBS+=("${f}")
    done < <(absl_dso_paths)
    log "link line: ${BRPC_LINK_LIBS[*]}"
}

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
    gcc_version="$(c++ --version 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "compiler_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# prebuilt 模式:镜像只提供已安装的 brpc;测试需要上游源码树以复用官方 example/echo_c++。
# 找不到镜像内置源码树时浅克隆一份(仅为拿 example/,不重新构建库)。
ensure_source_tree() {
    local tree
    if tree="$(resolve_brpc_source_tree)"; then
        BRPC_SOURCE_TREE="${tree}"
        export BRPC_SOURCE_TREE
        log "using brpc source tree at ${BRPC_SOURCE_TREE}"
        return 0
    fi
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream example"
        return 40
    }
    tree="${PERF_WORK_DIR}/brpc-src"
    rm -rf -- "${tree}"
    log "cloning brpc ${SOFTWARE_VERSION} source for the upstream echo example"
    GIT_TERMINAL_PROMPT=0 git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${BRPC_SOURCE_URL}" "${tree}" || {
        log "ERROR: failed to clone brpc source for ${SOFTWARE_VERSION}"
        return 30
    }
    BRPC_SOURCE_TREE="${tree}"
    export BRPC_SOURCE_TREE
}

# 编译上游 example/echo_c++ 的 server/client(手动 protoc + 链接,避免依赖 example 的构建脚本)。
compile_echo_example() {
    local src_tree="$1"
    local example_dir="${src_tree}/example/echo_c++"
    local work="${SERVICE_DIR}/echo_build"
    [[ -d "${example_dir}" ]] || {
        log "ERROR: upstream example/echo_c++ not found: ${example_dir}"
        return 40
    }
    [[ -f "${example_dir}/echo.proto" && -f "${example_dir}/server.cpp" && -f "${example_dir}/client.cpp" ]] || {
        log "ERROR: upstream echo example sources are incomplete in ${example_dir}"
        return 40
    }
    rm -rf -- "${work}"
    mkdir -p "${work}" "${SERVICE_DIR}"
    cp "${example_dir}/echo.proto" "${example_dir}/server.cpp" "${example_dir}/client.cpp" "${work}/"
    (
        cd "${work}"
        protoc --cpp_out=. echo.proto
        c++ -std=c++17 -O2 -I. -I"${BRPC_INCLUDE_DIR}" -o "${BRPC_ECHO_SERVER_BIN}" \
            server.cpp echo.pb.cc -L"${BRPC_LIB_DIR}" "${BRPC_LINK_LIBS[@]}"
        c++ -std=c++17 -O2 -I. -I"${BRPC_INCLUDE_DIR}" -o "${BRPC_ECHO_CLIENT_BIN}" \
            client.cpp echo.pb.cc -L"${BRPC_LIB_DIR}" "${BRPC_LINK_LIBS[@]}"
    ) || {
        log "ERROR: failed to compile the upstream echo example"
        return 40
    }
    log "compiled the upstream echo example (server/client)"
}

# 编译一个仅链接 libbrpc 的最小程序,验证头文件与静态库可用。
compile_link_smoke() {
    mkdir -p "${HARNESS_DIR}"
    cat >"${HARNESS_DIR}/link_smoke.cpp" <<'CPP'
#include <brpc/server.h>
#include <brpc/channel.h>

int main() {
    brpc::Server server;
    brpc::Channel channel;
    brpc::ServerOptions options;
    (void)options;
    return 0;
}
CPP
    c++ -std=c++17 -O2 -I"${BRPC_INCLUDE_DIR}" -o "${BRPC_LINK_SMOKE_BIN}" \
        "${HARNESS_DIR}/link_smoke.cpp" -L"${BRPC_LIB_DIR}" "${BRPC_LINK_LIBS[@]}" || {
        log "ERROR: failed to link the minimal brpc smoke program"
        return 40
    }
    log "compiled the minimal brpc link smoke program"
}

# ---------- 分层获取(build 模式) ----------
# 按优先级依次尝试,每层以 detect_brpc_version 强校验 == SOFTWARE_VERSION,不匹配即降级到下一层:
#   1. 上游官方二进制:brpc 官方 release 仅源码、无预编译产物(config/distribution.yaml: arm64_binary=na) → 本层不可行。
#   2. 三方仓库已打包二进制/包:发行版仓库无 brpc 包(apt=null),仅 conda-forge/brpc,而 build 模式
#      基础镜像无 conda 且版本通常滞后于请求版本 → 本层不可行。
#   3. 源码编译:从上游源码 cmake 构建(当前唯一可行层)。
acquire_brpc_upstream_binary() {
    log "upstream brpc ships source only (arm64_binary=na); no official binary layer"
    return 1
}

acquire_brpc_from_repo() {
    log "no distro brpc package (apt=null) and conda is absent in the build-mode image; repository layer cannot meet the exact-version gate"
    return 1
}

build_brpc_from_source() {
    local actual
    require_build_commands || return $?
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: source directory is not clean: ${SOURCE_DIR}"
        return 20
    }
    log "cloning brpc ${SOFTWARE_VERSION} from ${BRPC_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${BRPC_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone brpc ${SOFTWARE_VERSION}"
        return 30
    }
    actual="$(detect_brpc_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: source RELEASE_VERSION '${actual:-unknown}' does not match ${SOFTWARE_VERSION}"
        return 40
    }
    log "building the brpc library with cmake (Release)"
    mkdir -p "${BUILD_DIR}"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_DEBUG_SYMBOLS=OFF \
        -DBUILD_BRPC_TOOLS=OFF || {
        log "ERROR: cmake configure of brpc failed"
        return 40
    }
    cmake --build "${BUILD_DIR}" -j "$(nproc)" || {
        log "ERROR: cmake build of brpc failed"
        return 40
    }
    [[ -f "${BRPC_LIB_DIR}/libbrpc.a" || -f "${BRPC_LIB_DIR}/libbrpc.so" ]] || {
        log "ERROR: libbrpc was not produced under ${BRPC_LIB_DIR}"
        return 40
    }
    BRPC_SOURCE_TREE="${SOURCE_DIR}"
    export BRPC_SOURCE_TREE
    return 0
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:校验版本,并准备上游源码树以编译官方 example。
        require_compile_commands || return $?
        actual="$(detect_brpc_version || true)"
        [[ -n "${actual}" ]] || {
            log "ERROR: cannot determine the brpc version from the software image"
            return 40
        }
        ensure_source_tree || return $?
    else
        # 分层获取:上游二进制/仓库包均不可行时以源码编译为准。
        acquire_brpc_upstream_binary || {
            log "brpc upstream-binary layer infeasible; trying the repository layer"
            acquire_brpc_from_repo || {
                log "brpc repository layer infeasible; building from source"
                build_brpc_from_source || return $?
            }
        }
    fi

    build_link_libs
    compile_echo_example "${BRPC_SOURCE_TREE}" || return $?
    compile_link_smoke || return $?
    actual="$(detect_brpc_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: brpc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "brpc ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare brpc for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    brpc version (default: ${SOFTWARE_VERSION})
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