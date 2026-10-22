#!/usr/bin/env bash
# Protobuf 构建脚本:环境采集 + 准备被测软件(构建或定位)。
#   FUNC_MODE=prebuilt - 定位镜像内 protoc/includes,校验版本
#   FUNC_MODE=build    - 分层优先获取:官方二进制 → 三方精确版本包 → 源码编译
#
# build 模式的分层优先获取(优先级从高到低,每层产物都校验版本 == SOFTWARE_VERSION):
#   层1 上游官方二进制:官方 release 资产 protoc-<version>-linux-<arch>.zip,内含 bin/protoc 与
#         include/google/protobuf/*.proto。注意:上游只发布编译器前端,不发布 libprotobuf C++ 运行库
#         二进制,故层1 满足 protoc,libprotobuf 仍需层2/层3 补齐(功能用例的 cpp-roundtrip 要链接它)。
#   层2 三方仓库已打包的精确版本包:apt libprotobuf-dev / protobuf-compiler(基础镜像无 conda 渠道),
#         仅当候选版本严格等于 SOFTWARE_VERSION 时才采用。
#   层3 源码编译:兜底。层1 已提供 protoc 时只编译 libprotobuf(复用官方 protoc 自举);否则完整编译。
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

# 从 runner 离线目录取包,否则经可选 GitHub 代理直连下载。
copy_or_download_file() {
    local url="$1" asset_name="$2" output="$3"
    local download_url local_file
    local_file="${PROTOBUF_OFFLINE_DIR}/${asset_name}"
    if [[ -f "${local_file}" ]]; then
        log "using local release asset ${local_file}"
        cp "${local_file}" "${output}" || return 30
    else
        download_url="${url}"
        if [[ -n "${PERF_GITHUB_DOWNLOAD_PROXY}" ]]; then
            download_url="${PERF_GITHUB_DOWNLOAD_PROXY%/}/${url}"
        fi
        log "downloading ${url}"
        curl -fsSL --retry 3 --connect-timeout 30 -o "${output}" "${download_url}" || {
            log "ERROR: download failed: ${url}"
            return 30
        }
    fi
}

# 层1:上游官方 protoc 预编译二进制 release 资产 protoc-<version>-linux-<arch>.zip。
#   产物:bin/protoc + include/google/protobuf/*.proto(仅编译器前端,不含 libprotobuf 运行库)。
acquire_protoc_official_binary() {
    local arch asset url archive actual
    arch="$(normalize_arch "${EXPECTED_ARCH}")"
    case "${arch}" in
        x86_64) asset="protoc-${SOFTWARE_VERSION}-linux-x86_64.zip" ;;
        aarch64) asset="protoc-${SOFTWARE_VERSION}-linux-aarch_64.zip" ;;
        *)
            log "layer1: no official protoc asset is declared for ${EXPECTED_ARCH}"
            return 10
            ;;
    esac
    url="${PROTOBUF_RELEASE_BASE_URL}/v${SOFTWARE_VERSION}/${asset}"
    archive="${PERF_WORK_DIR}/${asset}"
    copy_or_download_file "${url}" "${asset}" "${archive}" || return $?
    rm -rf -- "${INSTALL_DIR}"
    mkdir -p "${INSTALL_DIR}" || return 30
    unzip -q -o "${archive}" -d "${INSTALL_DIR}" || {
        log "ERROR: failed to unzip ${asset}"
        return 40
    }
    PROTOC_BIN="${INSTALL_DIR}/bin/protoc"
    PROTOBUF_INC="${INSTALL_DIR}/include"
    PROTOBUF_CMAKE_PREFIX="${INSTALL_DIR}"
    [[ -x "${PROTOC_BIN}" ]] || {
        log "layer1: protoc is not present inside ${asset}"
        return 40
    }
    [[ -f "${PROTOBUF_INC}/google/protobuf/descriptor.proto" ]] || {
        log "layer1: google/protobuf/descriptor.proto is not present inside ${asset}"
        return 40
    }
    actual="$(read_protoc_version "${PROTOC_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "layer1: protoc ${actual:-unknown} != ${SOFTWARE_VERSION}, downgrade"
        return 40
    }
    return 0
}

# 层2:三方仓库(apt)中版本与请求版本严格一致的 protobuf 包。
#   发行版仓库的 protobuf 版本通常远落后于请求版本,仅在候选版本严格相等时采用。
acquire_protobuf_package() {
    local candidate new_protoc new_inc
    command -v apt-cache >/dev/null 2>&1 || {
        log "layer2: apt-cache is unavailable"
        return 10
    }
    candidate="$(apt-cache policy libprotobuf-dev protobuf-compiler 2>/dev/null \
        | sed -n 's/^[[:space:]]*Candidate:[[:space:]]*//p' | head -n 1)"
    candidate="${candidate%%-*}"
    [[ "${candidate}" == "${SOFTWARE_VERSION}" ]] || {
        log "layer2: available protobuf package '${candidate:-none}' != ${SOFTWARE_VERSION}, downgrade"
        return 10
    }
    apt_install libprotobuf-dev protobuf-compiler || return 30
    # 校验通过后再写全局变量,避免失败时污染层1/层3 的路径。
    new_protoc="$(command -v protoc 2>/dev/null || true)"
    new_inc="$(detect_protobuf_include_root || true)"
    [[ -x "${new_protoc}" ]] || {
        log "layer2: protoc is missing after package install"
        return 40
    }
    [[ -n "${new_inc}" && -f "${new_inc}/google/protobuf/descriptor.proto" ]] || {
        log "layer2: protobuf headers are missing after package install"
        return 40
    }
    [[ "$(read_protoc_version "${new_protoc}")" == "${SOFTWARE_VERSION}" ]] || {
        log "layer2: installed protoc version mismatch, downgrade"
        return 40
    }
    PROTOC_BIN="${new_protoc}"
    PROTOBUF_INC="${new_inc}"
    PROTOBUF_CMAKE_PREFIX="/usr"
    return 0
}

# 层3(补齐库):层1 已提供官方 protoc 时,仅源码编译 libprotobuf(及其头文件)装入同一前缀,
#   复用层1 的 protoc 自举;protoc/libprotoc 二进制不再构建(BUILD_PROTOC_BINARIES=OFF)。
#   失败时由调用方回退到完整源码构建。
build_libprotobuf_from_source() {
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream source"
        return 40
    }
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    log "cloning protobuf v${SOFTWARE_VERSION} from ${PROTOBUF_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${PROTOBUF_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone protobuf v${SOFTWARE_VERSION}"
        return 30
    }
    log "building libprotobuf only (protoc provided by layer1)"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -Dprotobuf_BUILD_TESTS=OFF \
        -Dprotobuf_BUILD_SHARED_LIBS=OFF \
        -Dprotobuf_FORCE_FETCH_DEPENDENCIES=ON \
        -Dprotobuf_BUILD_PROTOC_BINARIES=OFF \
        -Dprotobuf_BUILD_LIBPROTOC=OFF \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" || {
        log "ERROR: upstream CMake configuration failed"
        return 40
    }
    cmake --build "${BUILD_DIR}" -j"$(nproc)" || {
        log "ERROR: upstream CMake build failed"
        return 40
    }
    cmake --install "${BUILD_DIR}" || {
        log "ERROR: private protobuf installation failed"
        return 40
    }
}

build_protobuf() {
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the upstream source"
        return 40
    }
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    # 层1 失败时可能留下残缺的 INSTALL_DIR,完整源码构建前先清干净。
    rm -rf -- "${INSTALL_DIR}"
    # 复位为 build 模式的默认安装路径(层1/层2 可能已改写这些变量)。
    PROTOC_BIN="${INSTALL_DIR}/bin/protoc"
    PROTOBUF_INC="${SOURCE_DIR}/src"
    PROTOBUF_CMAKE_PREFIX="${INSTALL_DIR}"
    log "cloning protobuf v${SOFTWARE_VERSION} from ${PROTOBUF_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${PROTOBUF_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone protobuf v${SOFTWARE_VERSION}"
        return 30
    }
    log "configuring protobuf (upstream CMake Release)"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -Dprotobuf_BUILD_TESTS=OFF \
        -Dprotobuf_BUILD_SHARED_LIBS=OFF \
        -Dprotobuf_FORCE_FETCH_DEPENDENCIES=ON \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" || {
        log "ERROR: upstream CMake configuration failed"
        return 40
    }
    log "building protobuf"
    cmake --build "${BUILD_DIR}" -j"$(nproc)" || {
        log "ERROR: upstream CMake build failed"
        return 40
    }
    log "installing protobuf into ${INSTALL_DIR}"
    cmake --install "${BUILD_DIR}" || {
        log "ERROR: private protobuf installation failed"
        return 40
    }
    [[ -x "${PROTOC_BIN}" ]] || {
        log "ERROR: private protoc was not installed: ${PROTOC_BIN}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        [[ -x "${PROTOC_BIN}" ]] || {
            log "ERROR: protoc is not present in the software image"
            return 40
        }
        [[ -n "${PROTOBUF_INC}" && -f "${PROTOBUF_INC}/google/protobuf/descriptor.proto" ]] || {
            log "ERROR: protobuf include directory with descriptor.proto was not found"
            return 40
        }
    else
        require_build_commands || return $?
        [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" ]] || {
            log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
            return 20
        }
        # 分层优先获取:层1(官方二进制)→ 层2(三方精确版本包)→ 层3(源码编译)。
        # 注意:层1 官方资产只含 protoc(编译器前端),不含 libprotobuf C++ 运行库,
        # 故层1 命中后仍需以层3 源码补齐 libprotobuf(复用层1 的 protoc)。
        if acquire_protoc_official_binary; then
            log "layer1 ok: official protoc ${SOFTWARE_VERSION} release asset"
            if acquire_protobuf_package; then
                log "layer2 ok: protobuf package ${SOFTWARE_VERSION}"
            elif build_libprotobuf_from_source; then
                log "layer3 ok: libprotobuf from source (protoc from layer1)"
            else
                log "layer3 lib-only build failed; retry full source build"
                rm -rf -- "${SOURCE_DIR}" "${BUILD_DIR}"
                build_protobuf || return $?
            fi
        elif acquire_protobuf_package; then
            log "layer2 ok: protobuf package ${SOFTWARE_VERSION}"
        else
            log "layer1/layer2 unavailable; layer3: full protobuf source build"
            build_protobuf || return $?
        fi
    fi

    actual="$(read_protoc_version "${PROTOC_BIN}")"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot read the protoc version output"
        return 40
    }
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: protoc ${actual}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "protobuf ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare protobuf for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    protobuf version (default: ${SOFTWARE_VERSION})
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