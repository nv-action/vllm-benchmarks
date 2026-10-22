#!/usr/bin/env bash
# Apache Flink 构建脚本:环境采集 + 准备被测软件(分层获取或校验镜像内版本)。
#   FUNC_MODE=prebuilt - 校验镜像内 Flink 版本,不构建
#   FUNC_MODE=build    - 装 JDK/下载工具 + 按优先级分层获取:
#                        1) 上游官方二进制 tarball  2) 三方仓库打包的同版本产物  3) 源码编译
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version java_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    java_version="$(java -version 2>&1 | head -n 1 || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")",
  "java_version": "$(json_escape "${java_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 声明每个受支持版本对应的官方发行包名与 SHA-512(与 software/Bigdata/flink 保持一致)。
flink_archive_for_version() {
    case "${SOFTWARE_VERSION}" in
        1.16.3)
            FLINK_ARCHIVE_NAME="flink-1.16.3-bin-scala_2.12.tgz"
            # SHA-512 取自 archive.apache.org/dist/flink/flink-1.16.3/*.sha512
            FLINK_ARCHIVE_SHA512="12c4333b3589bfe0ba9e2c394c1f71683d932c7733deba12d08dbf9cff6d4fd1cb3a3d08177b2382bccf6287637a224fb7ed0e612502a37efcd4b5520c275082"
            ;;
        1.17.1)
            FLINK_ARCHIVE_NAME="flink-1.17.1-bin-scala_2.12.tgz"
            # SHA-512 取自 archive.apache.org/dist/flink/flink-1.17.1/*.sha512
            FLINK_ARCHIVE_SHA512="e4a0318a730c46bdf134e5fa071b7b01c634e7f2f131fe7f6fd44e8cff573f48497c2f1deaa9508f12cc2ab0ebb23564acb6f2dacbb4bd61ff2527cad3abe129"
            ;;
        2.3.0)
            FLINK_ARCHIVE_NAME="flink-2.3.0-bin-scala_2.12.tgz"
            FLINK_ARCHIVE_SHA512="e5863767caeaa7c72e45fc62d45f7df9f435a1c83aed813ea550db39e9221194d148ea4a6c3bfb5604335974729c579d48c6c4c3eb43502e37310a0bf982462a"
            ;;
        *)
            log "ERROR: no verified Apache Flink binary release is declared for ${SOFTWARE_VERSION}"
            return 10
            ;;
    esac
}

verify_java_runtime() {
    local major allowed
    major="$(java -version 2>&1 | sed -nE '1{s/.*version "([0-9]+).*/\1/p;}')"
    # 官方 Java 运行时支持矩阵:1.18 起才支持 Java 17;2.x 默认 17。
    case "$(flink_java_major)" in
        11) allowed="8 11" ;;
        *)
            if [[ "${SOFTWARE_VERSION%%.*}" -eq 1 ]]; then
                allowed="8 11 17"
            else
                allowed="17 21"
            fi
            ;;
    esac
    case " ${allowed} " in
        *" ${major} "*) ;;
        *)
            log "ERROR: Apache Flink ${SOFTWARE_VERSION} requires Java (${allowed}); found: $(java -version 2>&1 | head -n 1)"
            return 30
            ;;
    esac
}

install_flink_distribution() {
    local archive="${PERF_WORK_DIR}/${FLINK_ARCHIVE_NAME}" actual_sha512 offline_archive mirror downloaded

    offline_archive="${FLINK_OFFLINE_DIR}/${FLINK_ARCHIVE_NAME}"
    if [[ -f "${offline_archive}" ]]; then
        log "using local Apache Flink archive ${offline_archive}"
        install -m 0644 "${offline_archive}" "${archive}" || return 30
    else
        # 镜像源按序尝试(走 PERF_PROXY),archive.apache.org 持有全部历史版本作最终兜底。
        local -a mirrors=()
        read -ra mirrors <<<"${FLINK_MIRROR_URLS}" || true
        mirrors+=("${FLINK_RELEASE_URL}")
        downloaded=0
        for mirror in "${mirrors[@]}"; do
            log "downloading Apache Flink ${SOFTWARE_VERSION} from ${mirror}"
            if download_file "${mirror}/flink-${SOFTWARE_VERSION}/${FLINK_ARCHIVE_NAME}" "${archive}"; then
                downloaded=1
                break
            fi
            log "download from ${mirror} failed; trying the next mirror"
        done
        [[ "${downloaded}" -eq 1 ]] || {
            log "ERROR: failed to download ${FLINK_ARCHIVE_NAME} from any mirror"
            return 30
        }
    fi
    actual_sha512="$(sha512sum "${archive}" | awk '{print $1}')"
    if [[ "${actual_sha512}" != "${FLINK_ARCHIVE_SHA512}" ]]; then
        log "ERROR: Apache Flink archive SHA-512 mismatch: ${FLINK_ARCHIVE_NAME}"
        return 30
    fi
    [[ ! -e "${FLINK_HOME}" ]] || {
        log "ERROR: Flink installation directory is not clean: ${FLINK_HOME}"
        return 20
    }
    mkdir -p "${FLINK_HOME}" || return 30
    if ! tar -xzf "${archive}" -C "${FLINK_HOME}" --strip-components=1; then
        log "ERROR: failed to extract ${FLINK_ARCHIVE_NAME}"
        return 30
    fi
    rm -f "${archive}"
}

# ---------- 分层获取(build 模式) ----------
# 优先级:1) 上游官方二进制 tarball  2) 三方仓库打包的同版本产物  3) 源码编译。
# 每层获取后都用 bin/flink --version 校验版本 == SOFTWARE_VERSION;
# 不匹配或获取失败即降级到下一层,三层都不可用才让 install 失败。

# 读取当前 FLINK_HOME 里 Flink 的实际版本(读不到时返回非零)。
flink_installed_version() {
    local home="${1:-${FLINK_HOME}}" actual raw
    [[ -x "${home}/bin/flink" ]] || return 1
    # 统一 2>&1 合并输出后解析,避免上游把版本信息改到 stderr 时解析落空。
    raw="$("${home}/bin/flink" --version 2>&1 || true)"
    actual="$(printf '%s\n' "${raw}" | sed -n 's/^Version: *\([^,]*\).*/\1/p' | head -n 1 || true)"
    [[ -n "${actual}" ]] || actual="$(printf '%s\n' "${raw}" | sed -n 's/.*Flink \([0-9][0-9.]*\).*/\1/p' | head -n 1 || true)"
    [[ -n "${actual}" ]] || return 1
    printf '%s\n' "${actual}"
}

flink_version_matches() {
    [[ "$(flink_installed_version || true)" == "${SOFTWARE_VERSION}" ]]
}

# 层 2:三方仓库打包的同版本二进制/包。
#   Flink 官方只以二进制 tarball 与容器镜像(flink)分发;build 模式运行在普通容器内,
#   没有嵌套 docker/podman,官方镜像无法直接使用,故本层改用与请求版本一致的 Python
#   打包产物 apache-flink(第三方/官方在 PyPI 的打包发行)作为等效三方渠道,
#   从中定位其运行时 bin/flink。若该包未提供 bin/flink 或版本不符,本层判失败并降级到源码编译。
install_flink_from_package() {
    local target="${PERF_WORK_DIR}/flink-pypi" home
    command -v python3 >/dev/null 2>&1 || {
        log "layer 2: python3 is required to fetch the packaged Flink"
        return 30
    }
    rm -rf -- "${target}"
    mkdir -p "${target}" || return 30
    log "layer 2: fetching packaged Flink ${SOFTWARE_VERSION} (${FLINK_PYPI_PACKAGE})"
    if ! python3 -m pip install --disable-pip-version-check --no-deps --target "${target}" \
        "${FLINK_PYPI_PACKAGE}==${SOFTWARE_VERSION}"; then
        log "layer 2: ${FLINK_PYPI_PACKAGE}==${SOFTWARE_VERSION} is not available from the package index"
        return 30
    fi
    home="$(find "${target}" -type f -path '*/bin/flink' 2>/dev/null | sort | head -n 1 || true)"
    [[ -n "${home}" ]] || {
        log "layer 2: packaged Flink ships no bin/flink runtime"
        return 30
    }
    home="$(cd "$(dirname "${home}")/.." && pwd)"
    rm -rf -- "${FLINK_HOME}"
    mkdir -p "${FLINK_HOME}" || {
        log "layer 2: failed to create ${FLINK_HOME}"
        return 30
    }
    if ! cp -a "${home}/." "${FLINK_HOME}/"; then
        # 失败不再静默:带上磁盘水位,便于判断是不是空间/IO 问题。
        log "layer 2: failed to copy the packaged Flink into ${FLINK_HOME}; free space: $(df -h "${PERF_WORK_DIR}" 2>/dev/null | tail -n 1 || true)"
        return 30
    fi
    chmod +x "${FLINK_HOME}/bin/flink" 2>/dev/null || true
}

# 层 3 前置:源码编译需要 git 克隆 + Maven 构建;缺失时按发行版安装,不再直接放弃。
ensure_source_build_tools() {
    local -a missing=()
    command -v git >/dev/null 2>&1 || missing+=(git)
    command -v mvn >/dev/null 2>&1 || missing+=(maven)
    (( ${#missing[@]} == 0 )) && return 0
    log "layer 3: installing source build tools: ${missing[*]}"
    install_os_packages "${missing[@]}" || return 30
}

# 层 3:源码编译(最后手段)。按 release-<version> 分支克隆上游源码,用 Maven 构建发行版。
install_flink_from_source() {
    local src="${PERF_WORK_DIR}/flink-src" home
    ensure_source_build_tools || return $?
    rm -rf -- "${src}"
    log "layer 3: building Flink ${SOFTWARE_VERSION} from source (${FLINK_SOURCE_URL})"
    if ! GIT_TERMINAL_PROMPT=0 git clone --branch "release-${SOFTWARE_VERSION}" --depth 1 \
        "${FLINK_SOURCE_URL}" "${src}"; then
        log "layer 3: no source ref release-${SOFTWARE_VERSION} at ${FLINK_SOURCE_URL}"
        return 30
    fi
    if ! (cd "${src}" && mvn -q -DskipTests -Dfast -Dcheckstyle.skip -Drat.skip=true clean package); then
        log "layer 3: Maven build of Flink ${SOFTWARE_VERSION} failed"
        return 30
    fi
    home="$(find "${src}/flink-dist/target" -type f -path '*/bin/flink' 2>/dev/null | sort | head -n 1 || true)"
    [[ -n "${home}" ]] || { log "layer 3: source build produced no bin/flink distribution"; return 30; }
    home="$(cd "$(dirname "${home}")/.." && pwd)"
    rm -rf -- "${FLINK_HOME}"
    mkdir -p "${FLINK_HOME}" || return 30
    cp -a "${home}/." "${FLINK_HOME}/" || return 30
}

# build 模式分层安装:任一层通过 bin/flink --version 版本校验即停止。
install_flink_build() {
    if flink_archive_for_version && install_flink_distribution && flink_version_matches; then
        log "build layer 1/3 satisfied Apache Flink ${SOFTWARE_VERSION} (upstream tarball)"
        return 0
    fi
    log "build layer 1/3 did not provide Apache Flink ${SOFTWARE_VERSION}; degrading"
    rm -rf -- "${FLINK_HOME}"
    if install_flink_from_package && flink_version_matches; then
        log "build layer 2/3 satisfied Apache Flink ${SOFTWARE_VERSION} (packaged binary)"
        return 0
    fi
    log "build layer 2/3 did not provide Apache Flink ${SOFTWARE_VERSION}; degrading"
    rm -rf -- "${FLINK_HOME}"
    if install_flink_from_source && flink_version_matches; then
        log "build layer 3/3 satisfied Apache Flink ${SOFTWARE_VERSION} (source build)"
        return 0
    fi
    log "ERROR: no build layer provided Apache Flink ${SOFTWARE_VERSION}"
    rm -rf -- "${FLINK_HOME}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位并校验版本,不做任何构建。prebuilt 属于镜像验收,缺软件按镜像问题上报。
        FLINK_HOME="$(resolve_flink_home || true)"
        [[ -n "${FLINK_HOME}" && -x "${FLINK_HOME}/bin/flink" ]] || {
            log "ERROR: image problem: Apache Flink is not present in the software image"
            return 40
        }
        require_test_commands || return $?
    else
        require_build_commands || return $?
        verify_java_runtime || return $?
        install_flink_build || return $?
    fi

    actual="$(flink_installed_version || true)"
    if [[ "${actual}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: Apache Flink ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        # prebuilt 模式把 flink --version 的原始输出留给日志,便于定位镜像问题。
        if [[ -n "${FLINK_HOME}" && -x "${FLINK_HOME}/bin/flink" ]]; then
            "${FLINK_HOME}/bin/flink" --version 2>&1 | head -n 20 | sed 's/^/[flink-func]   /' || true
        fi
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "Apache Flink ${actual} ready at ${FLINK_HOME} (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare Apache Flink for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Flink version (default: ${SOFTWARE_VERSION})
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