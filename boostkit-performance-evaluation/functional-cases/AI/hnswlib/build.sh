#!/usr/bin/env bash
# hnswlib 构建脚本:环境采集 + 准备被测软件(C++ 头文件)。
#   FUNC_MODE=prebuilt - 使用镜像里已装好的 hnswlib 头文件,只做指纹校验
#   FUNC_MODE=build    - 分层获取并把头文件装到任务私有前缀:
#                        层1 上游官方 tarball → 层2 三方仓库(无)C++ 头文件包 → 层3 git 源码兜底
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

# 把源树里的 hnswlib/ 头文件目录安装到本次任务私有前缀。
install_headers_from() {
    local src_root="$1"
    [[ -d "${src_root}/hnswlib" ]] || {
        log "ERROR: source tree lacks the hnswlib/ header directory: ${src_root}"
        return 40
    }
    rm -rf -- "${HNSWLIB_PREFIX}"
    mkdir -p "${HNSWLIB_PREFIX}/include" || return 30
    cp -a "${src_root}/hnswlib" "${HNSWLIB_PREFIX}/include/hnswlib" || {
        log "ERROR: failed to install the hnswlib headers into ${HNSWLIB_PREFIX}"
        return 30
    }
    [[ -f "${HNSWLIB_PREFIX}/include/hnswlib/hnswlib.h" ]] || {
        log "ERROR: hnswlib.h is missing after install"
        return 40
    }
    return 0
}

# 层 1:上游官方 tarball(纯头文件,与架构无关)。
install_hnswlib_layer_upstream_tarball() {
    local archive extract_dir top
    if [[ -e "${HNSWLIB_PREFIX}" ]]; then
        log "ERROR: installation prefix is not clean: ${HNSWLIB_PREFIX}"
        return 20
    fi
    archive="${PERF_WORK_DIR}/hnswlib-${SOFTWARE_VERSION}.tar.gz"
    extract_dir="${PERF_WORK_DIR}/hnswlib-extract"
    log "layer1: downloading the official hnswlib v${SOFTWARE_VERSION} tarball"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${HNSWLIB_ARCHIVE_BASE}/v${SOFTWARE_VERSION}.tar.gz" || {
        log "layer1: failed to download the hnswlib v${SOFTWARE_VERSION} tarball"
        return 30
    }
    top="$(tar -tzf "${archive}" | head -n 1 | cut -d/ -f1 || true)"
    [[ -n "${top}" ]] || {
        log "layer1: unexpected tarball layout"
        return 40
    }
    rm -rf -- "${extract_dir}"
    mkdir -p "${extract_dir}" || return 30
    tar -xzf "${archive}" -C "${extract_dir}" || {
        log "layer1: failed to extract the tarball"
        return 40
    }
    rm -f "${archive}"
    install_headers_from "${extract_dir}/${top}" || return 40
    rm -rf -- "${extract_dir}"
    return 0
}

# 层 2:三方仓库已打包产物。
#   hnswlib 在发行版仓库里没有"C++ 头文件包"(conda-forge/hnswlib 属于 Python 绑定,
#   与本次的 C++ 验证形态不符);按分层设计显式跳过,交给下一层。
install_hnswlib_layer_repo_package() {
    log "layer2: no third-party channel ships the hnswlib C++ headers; skip"
    return 1
}

# 层 3:上游 git 源码 tag(兜底)。
install_hnswlib_layer_source_git() {
    local src="${PERF_WORK_DIR}/hnswlib-source"
    command -v git >/dev/null 2>&1 || {
        log "layer3: git is unavailable"
        return 1
    }
    rm -rf -- "${src}"
    log "layer3: cloning hnswlib v${SOFTWARE_VERSION} from ${HNSWLIB_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${HNSWLIB_SOURCE_URL}" "${src}" || {
        log "layer3: failed to clone hnswlib v${SOFTWARE_VERSION}"
        return 1
    }
    install_headers_from "${src}" || return 1
    rm -rf -- "${src}"
    return 0
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    # 指纹识别与编译探针都依赖 coreutils 的 sha256sum 与 C++ 编译器,两种模式都需要。
    require_check_commands || return $?
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:头文件在镜像里,只做指纹校验。
        [[ -n "${HNSWLIB_INCLUDE_DIR}" && -d "${HNSWLIB_INCLUDE_DIR}" ]] || {
            log "ERROR: hnswlib headers are not present in the software image"
            return 40
        }
        log "using the pre-installed hnswlib headers from ${HNSWLIB_INCLUDE_DIR}"
    else
        # 当场取源码:分层获取头文件到任务私有前缀。
        require_build_commands || return $?
        acquire_build_layers \
            install_hnswlib_layer_upstream_tarball \
            install_hnswlib_layer_repo_package \
            install_hnswlib_layer_source_git || return $?
    fi

    actual="$(read_installed_version)"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot identify the installed hnswlib version from ${HNSWLIB_INCLUDE_DIR:-<none>}"
        log_headers_diagnostic "${HNSWLIB_INCLUDE_DIR:-}"
        return 40
    }
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: hnswlib ${actual}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "hnswlib ${actual} ready (mode=${FUNC_MODE}, headers=${HNSWLIB_INCLUDE_DIR})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare hnswlib (C++ headers) for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    hnswlib version (default: ${SOFTWARE_VERSION})
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
