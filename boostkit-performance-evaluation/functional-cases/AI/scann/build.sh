#!/usr/bin/env bash
# scann 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 镜像内已装好 ScaNN,只定位并校验版本,不安装
#   FUNC_MODE=build    - 装依赖 + 分层优先获取(上游官方 wheel→三方仓库)到任务私有 venv
#                        (官方 aarch64 wheel 只有 cp39 是真实构建,故用 uv 拉固定 3.9 解释器来装)
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

# 安装上游官方 uv(astral-sh)静态二进制到本任务私有目录,用于拉取固定版本的 CPython。
install_uv() {
    local target archive
    [[ -x "${UV_BIN}" ]] && return 0
    case "$(normalize_arch "${EXPECTED_ARCH}")" in
        aarch64) target="aarch64-unknown-linux-gnu" ;;
        x86_64) target="x86_64-unknown-linux-gnu" ;;
        *)
            log "ERROR: unsupported architecture for uv: ${EXPECTED_ARCH}"
            return 30
            ;;
    esac
    archive="${PERF_WORK_DIR}/uv.tar.gz"
    log "downloading official uv ${UV_VERSION} (${target})"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${UV_RELEASE_BASE}/${UV_VERSION}/uv-${target}.tar.gz" || {
        log "ERROR: failed to download uv ${UV_VERSION}"
        return 30
    }
    rm -rf -- "${UV_DIR}"
    mkdir -p "${UV_DIR}" || return 30
    tar -xzf "${archive}" -C "${UV_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract uv"
        return 30
    }
    rm -f "${archive}"
    [[ -x "${UV_BIN}" ]] || {
        log "ERROR: uv binary is unavailable after install: ${UV_BIN}"
        return 30
    }
    return 0
}

# 用 uv 拉取固定版本 CPython 并据此创建任务私有 venv。
# scann 1.4.2 的 aarch64 包只有 cp39 是真实构建(cp310-cp313 均为同一产物副本,见上游 issue #2968),
# 解释器必须与 wheel 的真实编译版本一致,故锁定 3.9;
# ubuntu 26.04 自带 3.14 会选不到任何 wheel,而 venv 无法改变解释器版本,必须用独立构建。
create_scann_venv() {
    local actual_python
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_DIR}"
    export UV_PYTHON_PREFERENCE=only-managed
    if [[ -e "${VENV_DIR}" ]]; then
        log "ERROR: virtual environment is not clean: ${VENV_DIR}"
        return 20
    fi
    log "installing CPython ${SCANN_PYTHON_VERSION} via uv into ${UV_PYTHON_DIR}"
    "${UV_BIN}" python install "${SCANN_PYTHON_VERSION}" || {
        log "ERROR: uv failed to install CPython ${SCANN_PYTHON_VERSION}"
        return 40
    }
    log "creating task-private venv from CPython ${SCANN_PYTHON_VERSION}"
    # --seed 让 venv 自带 pip/setuptools,后续各层沿用 pip 安装产物。
    "${UV_BIN}" venv --python "${SCANN_PYTHON_VERSION}" --seed "${VENV_DIR}" || {
        log "ERROR: failed to create the virtual environment"
        return 40
    }
    [[ -x "${VENV_DIR}/bin/pip" ]] || {
        log "ERROR: the virtual environment has no usable pip"
        return 40
    }
    actual_python="$("${VENV_DIR}/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [[ "${actual_python}" == "${SCANN_PYTHON_VERSION}" ]] || {
        log "ERROR: venv python is ${actual_python}, expected ${SCANN_PYTHON_VERSION}"
        return 40
    }
    log "venv ready with CPython ${actual_python}"
}

# build 模式:先用 uv 拉固定版本 CPython 建任务私有 venv,再分层优先获取 ScaNN
# (见 common.sh acquire_build_layers)。
install_scann_build() {
    install_uv || return $?
    create_scann_venv || return $?
    acquire_build_layers \
        fetch_scann_upstream_binary \
        fetch_scann_third_party \
        fetch_scann_source || return $?
}

# 层 1:上游官方二进制。
# ScaNN 官方在 PyPI 发布预编译 wheel(config/distribution.yaml primary: pypi),aarch64 也有,
# 但只覆盖 cp39-cp313。venv 用的是固定 3.9 解释器,故这里命中官方 cp39 aarch64 wheel
# (aarch64 的 cp310-cp313 包是 cp39 的副本,不可用,详见上游 issue #2968);
# 强制 --only-binary=:all: 确保拿到二进制而非现场编译。
fetch_scann_upstream_binary() {
    local -a index_args=()
    mapfile -t index_args < <(pip_index_args)
    log "layer 1: installing the official prebuilt wheel ${SCANN_PACKAGE_SPEC}"
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" --only-binary=:all: "${SCANN_PACKAGE_SPEC}"
}

# 层 2:三方仓库已打包二进制。
# config/distribution.yaml 登记 ScaNN 的 conda/apt/docker 均为 null,无可用三方二进制渠道。
# 按分层设计显式跳过,交给下一层。
fetch_scann_third_party() {
    log "layer 2: scann has no third-party binary channel (conda/apt/docker null); skip"
    return 1
}

# 层 3:源码编译(最后手段)。
#   scann 上游从未发布 sdist(PyPI 上只有 bdist_wheel),且其源码构建依赖 Bazel 与 TensorFlow,
#   不适合在功能测试的 build 模式内现场编译;故本层结构性不可用,显式跳过。
fetch_scann_source() {
    log "layer 3: scann publishes no sdist and needs Bazel/TensorFlow to build; skip"
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
        log "using the pre-installed ScaNN from the software image"
    else
        install_scann_build || return $?
    fi

    local actual
    actual="$(read_installed_version)"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot detect the installed ScaNN version"
        return 40
    }
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: scann ${actual}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "ScaNN ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare ScaNN for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    ScaNN version (default: ${SOFTWARE_VERSION})
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