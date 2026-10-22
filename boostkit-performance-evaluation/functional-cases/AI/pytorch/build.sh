#!/usr/bin/env bash
# pytorch 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 镜像内已装好 PyTorch,只定位并校验版本,不安装
#   FUNC_MODE=build    - 装依赖 + 分层优先获取(官方 CPU wheel→三方 PyPI→源码编译)到任务私有 venv
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

# 官方 CPU wheel 仅覆盖支持矩阵内的解释器版本,新增 Python/版本需同步此处声明。
supported_python_version() {
    local python_version
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 30
    case "${python_version}" in
        3.10 | 3.11 | 3.12 | 3.13 | 3.14 | 3.15)
            printf '%s\n' "${python_version}"
            ;;
        *)
            log "ERROR: system python3 is ${python_version}, but the official PyTorch ${SOFTWARE_VERSION} CPU wheels require one of 3.10-3.15"
            return 30
            ;;
    esac
}

# build 模式:创建任务私有 venv,再分层优先获取 PyTorch(见 common.sh acquire_build_layers)。
install_pytorch_build() {
    [[ ! -e "${VENV_DIR}" ]] || {
        log "ERROR: virtual environment is not clean: ${VENV_DIR}"
        return 20
    }
    supported_python_version >/dev/null || return $?
    log "creating task-private virtual environment ${VENV_DIR}"
    python3 -m venv "${VENV_DIR}" || {
        log "ERROR: failed to create the virtual environment"
        return 40
    }
    [[ -x "${VENV_DIR}/bin/pip" ]] || {
        log "ERROR: the virtual environment has no usable pip"
        return 40
    }
    acquire_build_layers \
        fetch_torch_upstream_binary \
        fetch_torch_third_party \
        fetch_torch_source || return $?
}

# 层 1:上游官方二进制。
# PyTorch 官方在 download.pytorch.org/whl 发布 CPU aarch64/x86_64 wheel
# (config/distribution.yaml primary: upstream-tarball,arm64_binary: yes),
# 依赖包走华为云 PyPI 镜像。安装官方 ${SOFTWARE_VERSION}+cpu wheel。
fetch_torch_upstream_binary() {
    local -a index_args=()
    mapfile -t index_args < <(pip_index_args)
    log "layer 1: installing the official CPU wheel ${TORCH_PACKAGE_SPEC} from ${PYTORCH_WHEEL_INDEX}"
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" "${TORCH_PACKAGE_SPEC}"
}

# 层 2:三方仓库已打包二进制。
# PyPI 上的 torch(官方同步发布的 wheel;历史上以 +cpu 等本地版本后缀区分 CPU 构建)。
# 请求版本严格相等,故按 ${SOFTWARE_VERSION}+cpu 与 ${SOFTWARE_VERSION} 两种候选尝试。
fetch_torch_third_party() {
    local -a index_args=()
    mapfile -t index_args < <(pip_pypi_index_args)
    log "layer 2: installing the equivalent PyPI wheel ${TORCH_PACKAGE_SPEC} / ${TORCH_PACKAGE_PLAIN_SPEC}"
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" --only-binary=:all: \
        "${TORCH_PACKAGE_SPEC}" "${TORCH_PACKAGE_PLAIN_SPEC}" && return 0
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" --only-binary=:all: "${TORCH_PACKAGE_PLAIN_SPEC}"
}

# 层 3:源码编译(最后手段)。
#   PyTorch 从源码编译需要数小时与 20 GiB 以上的构建空间,而本用例的 timeout 是 120 分钟,
#   该层在功能测试内不可能成功,故结构性不可用。此前为它把磁盘预检抬到 25 GiB,
#   结果反而拦住了实际可用的官方 wheel 路径(见 func_case.yaml 的 min_free_disk_gb)。
fetch_torch_source() {
    log "layer 3: a PyTorch source build needs hours and >20 GiB of space; out of scope here"
    return 1
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_python_runtime || return $?

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -n "${PYBIN}" && -x "${PYBIN}" ]] || {
            log "ERROR: python3 is unavailable in the software image"
            return 40
        }
        log "using the pre-installed PyTorch from the software image"
    else
        install_pytorch_build || return $?
    fi

    local actual
    actual="$(read_installed_version)"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot detect the installed PyTorch version"
        return 40
    }
    [[ "${actual}" == "$(torch_expected_version)" ]] || {
        log "ERROR: torch ${actual}, expected $(torch_expected_version)"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "PyTorch ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare PyTorch for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    PyTorch version (default: ${SOFTWARE_VERSION})
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