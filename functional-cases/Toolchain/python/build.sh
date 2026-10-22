#!/usr/bin/env bash
# python (CPython) 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 python3 版本,不构建
#   FUNC_MODE=build    - 装依赖 + 分层获取并校验版本:
#                        层1 上游官方二进制(官方仅源码,不可用)→ 层2 python-build-standalone
#                        → 层3 三方仓库包 → 层4 源码编译
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

# 下载官方 CPython 源码 tarball 并解压到私有源码目录(不动系统 Python)。
prepare_cpython_source() {
    local tarball="${PERF_WORK_DIR}/Python-${SOFTWARE_VERSION}.tgz"
    if [[ -e "${PYTHON_SOURCE_DIR}" ]]; then
        log "ERROR: source directory is not clean: ${PYTHON_SOURCE_DIR}"
        return 20
    fi
    log "downloading official CPython source Python-${SOFTWARE_VERSION}.tgz"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${tarball}" \
        "${PYTHON_SOURCE_URL}/${SOFTWARE_VERSION}/Python-${SOFTWARE_VERSION}.tgz" || {
        log "ERROR: failed to download CPython ${SOFTWARE_VERSION} source"
        return 30
    }
    mkdir -p "${PYTHON_SOURCE_DIR}" || return 30
    tar -xzf "${tarball}" -C "${PYTHON_SOURCE_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract the CPython source tarball"
        return 30
    }
    rm -f "${tarball}"
    [[ -f "${PYTHON_SOURCE_DIR}/configure" ]] || {
        log "ERROR: extracted CPython source has no configure script"
        return 30
    }
}

# 构建并安装到私有前缀,然后校验版本与关键模块。
build_python_from_source() {
    local -a configure_options
    local version_output actual_version

    prepare_cpython_source || return $?
    read -r -a configure_options <<<"${CONFIGURE_OPTIONS}"
    (( ${#configure_options[@]} > 0 )) || {
        log "ERROR: CPython configure options are empty"
        return 40
    }
    log "configuring CPython ${SOFTWARE_VERSION} with ${CONFIGURE_OPTIONS}"
    (
        cd "${PYTHON_SOURCE_DIR}"
        ./configure --prefix="${PYTHON_INSTALL_DIR}" "${configure_options[@]}"
    ) || {
        log "ERROR: CPython configure failed"
        return 40
    }
    log "building CPython ${SOFTWARE_VERSION} with make -j$(nproc)"
    (
        cd "${PYTHON_SOURCE_DIR}"
        make -j"$(nproc)"
    ) || {
        log "ERROR: CPython make failed"
        return 40
    }
    log "installing CPython ${SOFTWARE_VERSION} into ${PYTHON_INSTALL_DIR}"
    (
        cd "${PYTHON_SOURCE_DIR}"
        make install
    ) || {
        log "ERROR: CPython make install failed"
        return 40
    }
    [[ -x "${PYTHON_BIN}" ]] || {
        log "ERROR: CPython interpreter was not installed: ${PYTHON_BIN}"
        return 40
    }
    version_output="$("${PYTHON_BIN}" --version 2>&1)" || {
        log "ERROR: built CPython cannot report its version"
        return 40
    }
    actual_version="${version_output#Python }"
    actual_version="${actual_version%%[[:space:]]*}"
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: built CPython reports ${actual_version:-unknown}, requested ${SOFTWARE_VERSION}"
        return 40
    }
    # 标准库关键模块必须可用(smoke 依赖 zlib,安装阶段额外确认 ssl)。
    "${PYTHON_BIN}" -c "import ssl, zlib, json, hashlib" || {
        log "ERROR: built CPython is missing required standard library modules"
        return 40
    }
    log "CPython ${SOFTWARE_VERSION} interpreter is ready at ${PYTHON_BIN}"
}

# ---------- 分层获取(build 模式) ----------
# 按优先级(高→低)依次尝试,每一层获取后都必须用 `python3 --version` 校验版本 == SOFTWARE_VERSION,
# 不匹配或获取失败即降级到下一层;四层都不可行才让 install 失败。
# 渠道依据 config/distribution.yaml 的 python 条目:primary=upstream-release(python.org),
# channels=python-build-standalone、apt python3、docker library/python。

# 层1:上游官方二进制 tarball。
#   python.org 在 Linux 仅分发源码 tarball,无官方预编译二进制 → 该层结构性不可用。
install_python_layer_upstream_binary() {
    log "layer1: python.org provides source only (no official Linux binary); skipping to layer 2"
    return 1
}

# 层2:python-build-standalone(astral 维护、uv 同源的 CPython 官方独立构建)。
#   资产名 cpython-<version>+<tag>-<triple>-install_only.tar.gz 版号精确到补丁位,
#   命中后无需源码编译;tag 是日期流水号,故按最新若干个 release 依次探测。
PBS_RELEASES_URL="${PBS_RELEASES_URL:-https://github.com/astral-sh/python-build-standalone/releases}"
PBS_TAG_SCAN="${PBS_TAG_SCAN:-10}"

pbs_platform_triple() {
    case "$(uname -m)" in
        aarch64 | arm64) printf 'aarch64-unknown-linux-gnu\n' ;;
        x86_64 | amd64) printf 'x86_64-unknown-linux-gnu\n' ;;
        *) return 1 ;;
    esac
}

# 最近的若干 release tag:直接解析 releases.atom,不调用 GitHub API,避免匿名限流。
# atom 不可达时退化为只探测最新 tag(靠 /releases/latest 的重定向取 tag 名)。
pbs_recent_tags() {
    local tags latest
    tags="$(curl -fsSL --retry 3 --connect-timeout 30 -A 'Mozilla/5.0' \
        "${PBS_RELEASES_URL}/releases.atom" 2>/dev/null |
        sed -n 's#.*releases/tag/\([^"<]*\)".*#\1#p' || true)"
    if [[ -n "${tags}" ]]; then
        printf '%s\n' "${tags}" | head -n "${PBS_TAG_SCAN}" || true
        return 0
    fi
    latest="$(curl -fsSL -o /dev/null -w '%{url_effective}' \
        "${PBS_RELEASES_URL}/latest" 2>/dev/null || true)"
    latest="${latest##*/}"
    [[ -n "${latest}" ]] && printf '%s\n' "${latest}"
    return 0
}

# 下载 install_only 资产并解压到私有前缀;版本或标准库模块不符即清理并继续下一个 tag。
install_python_layer_standalone() {
    local triple tag asset url candidate version
    triple="$(pbs_platform_triple)" || {
        log "layer2: architecture $(uname -m) has no python-build-standalone target"
        return 1
    }
    candidate="${PYTHON_INSTALL_DIR}/bin/python3"
    for tag in $(pbs_recent_tags); do
        asset="cpython-${SOFTWARE_VERSION}+${tag}-${triple}-install_only.tar.gz"
        url="${PBS_RELEASES_URL}/download/${tag}/${asset}"
        log "layer2: probing python-build-standalone ${tag}"
        rm -f -- "${PERF_WORK_DIR}/${asset}"
        if ! curl -fsSL --retry 2 --connect-timeout 30 -A 'Mozilla/5.0' \
            -o "${PERF_WORK_DIR}/${asset}" "${url}" 2>/dev/null; then
            continue
        fi
        mkdir -p "${PYTHON_INSTALL_DIR}"
        if ! tar -xzf "${PERF_WORK_DIR}/${asset}" -C "${PYTHON_INSTALL_DIR}" --strip-components=1; then
            log "layer2: failed to extract ${asset}"
            rm -rf -- "${PYTHON_INSTALL_DIR}" "${PERF_WORK_DIR}/${asset}"
            continue
        fi
        rm -f -- "${PERF_WORK_DIR}/${asset}"
        if [[ ! -x "${candidate}" ]]; then
            log "layer2: ${asset} has no bin/python3"
            rm -rf -- "${PYTHON_INSTALL_DIR}"
            continue
        fi
        version="$(python_version_of "${candidate}")"
        if [[ "${version}" != "${SOFTWARE_VERSION}" ]]; then
            log "layer2: ${asset} reports Python ${version:-unknown}, not ${SOFTWARE_VERSION}"
            rm -rf -- "${PYTHON_INSTALL_DIR}"
            continue
        fi
        # check 阶段要跑上游回归测试(test.regrtest),产物必须自带 test 包且标准库模块可用。
        if ! "${candidate}" -c "import ssl, zlib, json, hashlib, test.regrtest" >/dev/null 2>&1; then
            log "layer2: ${asset} cannot import the standard library modules required by the checks"
            rm -rf -- "${PYTHON_INSTALL_DIR}"
            continue
        fi
        printf 'PYTHON_BIN=%s\n' "${candidate}" >"${LAYERED_TOOL_FILE}"
        log "layer2: adopted python-build-standalone ${tag} (${candidate})"
        return 0
    done
    rm -rf -- "${PYTHON_INSTALL_DIR}"
    return 1
}

# 层3:三方仓库中与请求版本一致的 CPython 二进制/包。
#   build 模式无嵌套容器 → library/python 镜像不可用;基础镜像也未内置 conda →
#   改用等效的发行版仓库包(apt python3.14)。
install_python_layer_repo_package() {
    local package candidate version
    for package in "python${PYTHON_MM}" "python3"; do
        if ! apt_install "${package}" >/dev/null 2>&1; then
            log "layer3: distro package ${package} is unavailable"
            continue
        fi
        candidate="$(command -v "python${PYTHON_MM}" 2>/dev/null || command -v python3 2>/dev/null || true)"
        [[ -n "${candidate}" ]] || continue
        version="$(python_version_of "${candidate}")"
        if [[ "${version}" == "${SOFTWARE_VERSION}" ]]; then
            printf 'PYTHON_BIN=%s\n' "${candidate}" >"${LAYERED_TOOL_FILE}"
            log "layer3: adopted distro Python package ${package} (${candidate})"
            return 0
        fi
        log "layer3: distro package ${package} provides Python ${version:-unknown}, not ${SOFTWARE_VERSION}"
    done
    return 1
}

# 层4:从官方源码 tarball 编译(最后手段;CPython 官方在 Linux 仅分发源码)。
install_python_layer_source_build() {
    build_python_from_source
}

# 分层获取入口:依次尝试四层,任一层成功即返回 0。
install_python_layered() {
    if install_python_layer_upstream_binary; then
        log "acquired CPython via layer 1 (upstream official binary)"
        return 0
    fi
    log "layer 1 (upstream official binary) unavailable; falling back to layer 2"
    if install_python_layer_standalone; then
        log "acquired CPython via layer 2 (python-build-standalone)"
        return 0
    fi
    log "layer 2 (python-build-standalone) unavailable; falling back to layer 3"
    if install_python_layer_repo_package; then
        log "acquired CPython via layer 3 (third-party repo package)"
        return 0
    fi
    log "layer 3 (third-party repo package) unavailable; falling back to layer 4"
    if install_python_layer_source_build; then
        log "acquired CPython via layer 4 (source build)"
        return 0
    fi
    log "ERROR: no CPython ${SOFTWARE_VERSION} acquisition layer succeeded"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -x "${PYTHON_BIN}" ]] || {
            log "ERROR: python3 is not present in the software image"
            return 40
        }
    else
        # 当场构建:装依赖 + 分层获取(官方二进制→三方仓库包→官方源码编译到私有前缀)。
        require_build_commands || return $?
        [[ ! -e "${PYTHON_INSTALL_DIR}" ]] || {
            log "ERROR: installation directory is not clean: ${PYTHON_INSTALL_DIR}"
            return 20
        }
        install_python_layered || return $?
        # layer2 可能改选发行版包的 python,重新解析工具路径后再统一校验版本。
        configure_runtime_paths || return $?
    fi

    actual="$(python_version_of "${PYTHON_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: python ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "python ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare CPython for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    CPython version (default: ${SOFTWARE_VERSION})
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