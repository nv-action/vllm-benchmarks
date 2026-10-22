#!/usr/bin/env bash
# x265 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 x265 版本
#   FUNC_MODE=build    - 分层获取:上游无二进制、仓库包版本滞后,最终以源码编译(官方源码归档 + cmake)为准
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version cxx_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    cxx_version="$(c++ --version 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "compiler_version": "$(json_escape "${cxx_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# ---------- 分层获取(build 模式) ----------
# 按优先级依次尝试,每层以 read_x265_version 强校验(去本地后缀后)== SOFTWARE_VERSION,不匹配即降级到下一层:
#   1. 上游官方二进制:x265 上游仅源码、无预编译产物(config/distribution.yaml: arm64_binary=na) → 本层不可行。
#   2. 三方仓库已打包二进制/包:发行版仓库的 x265 包(apt=libx265-dev)与 conda-forge/x265 版本普遍滞后于
#      请求版本,且 build 模式基础镜像无 conda;按"版本严格相等"门槛无法命中 → 本层不可行。
#   3. 源码编译:下载官方源码归档并 cmake 构建(当前唯一可行层)。
acquire_x265_upstream_binary() {
    log "upstream x265 ships source only (arm64_binary=na); no official binary layer"
    return 1
}

acquire_x265_from_repo() {
    log "distro/conda x265 packages lag the requested ${SOFTWARE_VERSION} and conda is absent; repository layer cannot meet the exact-version gate"
    return 1
}

# build 模式:下载官方源码归档并 cmake 构建安装出 x265 CLI。
build_x265_from_source() {
    local archive_name archive_path
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: source directory is not clean: ${SOURCE_DIR}"
        return 20
    }
    archive_name="x265_${SOFTWARE_VERSION}.tar.gz"
    archive_path="${PERF_WORK_DIR}/${archive_name}"
    log "downloading official x265 source archive ${archive_name}"
    curl -fSL --connect-timeout 30 --retry 3 -o "${archive_path}" \
        "${X265_ARCHIVE_BASE_URL}/${archive_name}" || {
        log "ERROR: failed to download ${X265_ARCHIVE_BASE_URL}/${archive_name}"
        return 30
    }
    mkdir -p "${SOURCE_DIR}" || return 30
    tar -xzf "${archive_path}" -C "${SOURCE_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract ${archive_path}"
        return 30
    }
    [[ -d "${SOURCE_DIR}/source" ]] || {
        log "ERROR: x265 source directory was not extracted: ${SOURCE_DIR}"
        return 30
    }

    log "building and installing x265 into ${INSTALL_DIR}"
    mkdir -p "${BUILD_DIR}" "${INSTALL_DIR}"
    (
        cmake -S "${SOURCE_DIR}/source" -B "${BUILD_DIR}" \
            -DCMAKE_BUILD_TYPE=Release \
            -DENABLE_PIC=ON \
            -DENABLE_ASSEMBLY=ON \
            -DCMAKE_VERBOSE_MAKEFILE=ON \
            -DCMAKE_C_FLAGS=-O3 \
            -DCMAKE_CXX_FLAGS=-O3 \
            -DHIGH_BIT_DEPTH=OFF \
            -DENABLE_TESTS=ON \
            -DENABLE_SHARED=ON \
            -DENABLE_LIBNUMA=OFF \
            -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" || {
            log "ERROR: x265 cmake configure failed"
            exit 40
        }
        cmake --build "${BUILD_DIR}" -j"$(nproc)" &&
            cmake --install "${BUILD_DIR}" || {
            log "ERROR: x265 make or install failed"
            exit 40
        }
    ) || return $?

    [[ -x "${X265_BIN}" ]] || {
        log "ERROR: x265 CLI was not produced at ${X265_BIN}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_build_commands || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只校验可执行文件与版本。
        [[ -x "${X265_BIN}" ]] || {
            log "ERROR: x265 is not present in the software image (${X265_BIN})"
            return 40
        }
    else
        acquire_x265_upstream_binary || {
            log "x265 upstream-binary layer infeasible; trying the repository layer"
            acquire_x265_from_repo || {
                log "x265 repository layer infeasible; building from source"
                build_x265_from_source || return $?
            }
        }
    fi

    actual="$(read_x265_version || true)"
    [[ "$(base_x265_version "${actual}")" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: x265 ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "x265 ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare x265 for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    x265 version (default: ${SOFTWARE_VERSION})
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
