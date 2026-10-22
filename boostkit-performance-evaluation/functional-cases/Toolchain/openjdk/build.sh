#!/usr/bin/env bash
# openjdk (OpenJDK) 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 JDK 版本,不构建
#   FUNC_MODE=build    - 装依赖 + 分层获取并校验版本:
#                        层1 上游官方二进制(jdk.java.net)→ 层2 三方仓库包 → 层3 源码编译
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

# 层1:上游官方二进制。
# jdk.java.net/<feature>/ 会列出当前 GA 或 EA 的二进制(含 linux-aarch64/x64)并给出 .sha256。
# 页面结构可能随发行变动,故不硬编码 build 号,而是解析页面取当前 linux 二进制链接。
install_openjdk_official_binary() {
    local page url href filename archive sha_url expected actual feature
    feature="$(requested_feature)"
    if [[ -e "${JDK_HOME}" ]]; then
        log "ERROR: installation directory is not clean: ${JDK_HOME}"
        return 20
    fi
    log "layer1: querying ${JDK_PAGE_BASE}/${feature}/ for the linux-${JDK_ARCH} binary"
    page="$(curl -fsSL --retry 3 --connect-timeout 30 "${JDK_PAGE_BASE}/${feature}/")" || {
        log "layer1: failed to fetch the JDK download page"
        return 30
    }
    href="$(printf '%s\n' "${page}" \
        | grep -oE 'download\.java\.net[^"]*_linux-'"${JDK_ARCH}"'_bin\.tar\.gz' \
        | head -n 1 || true)"
    [[ -n "${href}" ]] || {
        log "layer1: no linux-${JDK_ARCH} binary is listed for JDK ${feature}"
        return 30
    }
    url="https://${href}"
    filename="$(basename "${href}")"
    archive="${PERF_WORK_DIR}/${filename}"
    log "layer1: downloading ${url}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" "${url}" || {
        log "layer1: failed to download ${filename}"
        return 30
    }
    # 官方同时发布 .sha256;能取到就校验,取不到则记录并继续(不阻塞安装)。
    sha_url="${url}.sha256"
    expected="$(curl -fsSL --retry 3 --connect-timeout 30 "${sha_url}" 2>/dev/null | awk '{print $1}' | head -n 1 || true)"
    if [[ -n "${expected}" ]]; then
        actual="$(sha256sum "${archive}" | awk '{print $1}')"
        [[ "${actual}" == "${expected}" ]] || {
            log "layer1: checksum mismatch for ${filename}"
            return 30
        }
    else
        log "layer1: no .sha256 is published for ${filename}; skipping checksum verification"
    fi
    mkdir -p "${JDK_HOME}" || return 30
    tar -xzf "${archive}" -C "${JDK_HOME}" --strip-components=1 || {
        log "layer1: failed to extract ${filename}"
        return 30
    }
    rm -f "${archive}"
    [[ -x "${JAVA_BIN}" && -x "${JAVAC_BIN}" ]] || {
        log "layer1: extracted JDK is missing bin/java or bin/javac"
        return 30
    }
    log "layer1: installed OpenJDK ${feature} from ${url}"
    return 0
}

# 层2:三方仓库已打包二进制。
#   OpenJDK 官方不在发行版仓库外分发 GA 前的版本,GA 后由 Adoptium 提供版本化下载;
#   这里按 Adoptium 的 API 取 Temurin 二进制(GA 之前该 API 返回 404,自然降级到层3)。
install_openjdk_layer_repo_package() {
    local api_url archive feature
    feature="$(requested_feature)"
    api_url="${ADOPTIUM_API_BASE}/${feature}/ga/linux/${JDK_ARCH}/jdk/hotspot/normal/eclipse?project=jdk"
    archive="${PERF_WORK_DIR}/temurin-${feature}.tar.gz"
    log "layer2: querying the Adoptium Temurin API for JDK ${feature}"
    if curl -fsSL --retry 2 --connect-timeout 30 -o "${archive}" "${api_url}"; then
        rm -rf -- "${JDK_HOME}"
        mkdir -p "${JDK_HOME}" || return 1
        if tar -xzf "${archive}" -C "${JDK_HOME}" --strip-components=1; then
            rm -f "${archive}"
            [[ -x "${JAVA_BIN}" && -x "${JAVAC_BIN}" ]] || return 1
            log "layer2: installed Temurin JDK ${feature} for ${JDK_ARCH}"
            return 0
        fi
        rm -f "${archive}"
    fi
    log "layer2: the Adoptium API has no Temurin build for JDK ${feature}; downgrade"
    return 1
}

# 层3:源码编译(最后手段)。
#   OpenJDK 源码构建需要引导 JDK,且单次构建耗时数小时,不适合放在功能测试的 build 模式内;
#   无官方二进制也无三方二进制时,明确判该层不可用。
install_openjdk_layer_source_build() {
    log "layer3: an OpenJDK source build needs a boot JDK and hours of time; out of scope here"
    return 1
}

# 分层获取入口:依次尝试三层,任一层成功即返回 0。
install_openjdk_layered() {
    if install_openjdk_official_binary; then
        log "acquired OpenJDK via layer 1 (upstream official binary)"
        return 0
    fi
    log "layer 1 (upstream official binary) unavailable; falling back to layer 2"
    if install_openjdk_layer_repo_package; then
        log "acquired OpenJDK via layer 2 (third-party repo package)"
        return 0
    fi
    log "layer 2 (third-party repo package) unavailable; falling back to layer 3"
    if install_openjdk_layer_source_build; then
        log "acquired OpenJDK via layer 3 (source build)"
        return 0
    fi
    log "ERROR: no OpenJDK ${SOFTWARE_VERSION} acquisition layer succeeded"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual feature requested
    requested="$(requested_feature)"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -x "${JAVA_BIN}" ]] || {
            log "ERROR: java is not present in the software image"
            return 40
        }
    else
        # 当场安装:装依赖 + 分层获取(jdk.java.net 官方二进制 → Adoptium → 源码编译)。
        require_build_commands || return $?
        install_openjdk_layered || return $?
    fi

    [[ -x "${JAVAC_BIN}" ]] || {
        log "ERROR: javac is unavailable: ${JAVAC_BIN:-unset}"
        return 40
    }
    actual="$(java_version_of "${JAVA_BIN}")"
    feature="$(java_feature_of "${JAVA_BIN}")"
    [[ "${feature}" == "${requested}" ]] || {
        log "ERROR: JDK feature ${feature:-unknown} (java ${actual:-unknown}), expected ${requested}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "openjdk ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare OpenJDK for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    JDK version: feature(27)或 feature_build(28_19),默认 ${SOFTWARE_VERSION}
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
