#!/usr/bin/env bash
# Envoy 构建脚本:环境采集 + 准备被测软件(下载或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 envoy 版本
#   FUNC_MODE=build    - 分层获取:官方 release 资产(优先)→ 仓库包/镜像 → 源码编译(逐层按版本强校验降级)
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 读取 envoy 版本号。`envoy --version` 形如:
#   envoy  version: <hash>/1.39.1/<build>/<ssl>...
read_envoy_version() {
    local line value
    [[ -x "${ENVOY_BIN}" ]] || return 1
    line="$("${ENVOY_BIN}" --version 2>/dev/null | head -n 1)"
    value="$(printf '%s\n' "${line}" | sed -n 's#.*/\([0-9][0-9.]*\)/.*#\1#p' | head -n 1)"
    if [[ -z "${value}" ]]; then
        value="$(printf '%s\n' "${line}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true)"
    fi
    printf '%s\n' "${value}"
}

# 优先使用 runner 离线目录中的官方资产,否则按 sha256 校验后下载。
copy_or_download_verified_file() {
    local url="$1" asset_name="$2" output="$3" expected_sha256="$4"
    local actual_sha256 download_url local_file
    local_file="${ENVOY_OFFLINE_DIR}/${asset_name}"
    if [[ -f "${local_file}" ]]; then
        log "using local release asset ${local_file}"
        install -m 0755 "${local_file}" "${output}" || return 30
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
    actual_sha256="$(sha256sum "${output}" | awk '{print $1}')"
    [[ "${actual_sha256}" == "${expected_sha256}" ]] || {
        log "ERROR: checksum mismatch for $(basename "${output}")"
        return 30
    }
}

install_release_artifacts() {
    local arch asset sha256 url suffix
    arch="$(normalize_arch "${EXPECTED_ARCH}")"
    case "${arch}" in
        x86_64) suffix="x86_64" ;;
        aarch64) suffix="aarch_64" ;;
        *)
            log "ERROR: no verified Envoy release asset is declared for ${EXPECTED_ARCH}"
            return 10
            ;;
    esac
    asset="envoy-${SOFTWARE_VERSION}-linux-${suffix}"
    # 官方资产按版本逐次变化,SHA-256 必须按 版本/架构 精确匹配,
    # 取值来自各 release 的 checksums.txt.asc。
    case "${SOFTWARE_VERSION}/${arch}" in
        1.40.0/x86_64) sha256="8aa137a7d00994ce43f613556f5a120709229f63bedb86eedde5ccbc402c2b80" ;;
        1.40.0/aarch64) sha256="03cacc952561635c0b7d2a829291f341dadbebc32050475efbe7321b1bbe18c2" ;;
        1.37.1/x86_64) sha256="8db90ee0aa085ae6873dbf21212816d29f6c455d4760c4aecf4d6593763db18c" ;;
        1.37.1/aarch64) sha256="65811e12ae8f79d4f4f6418d27a2d32febc4cc8a5233d5ae8a4da0e3e0f3fe75" ;;
        *)
            log "ERROR: no verified SHA-256 for envoy ${SOFTWARE_VERSION} on ${EXPECTED_ARCH}"
            return 10
            ;;
    esac
    mkdir -p "${INSTALL_DIR}" || return 30
    url="https://github.com/envoyproxy/envoy/releases/download/v${SOFTWARE_VERSION}/${asset}"
    copy_or_download_verified_file "${url}" "${asset}" "${ENVOY_BIN}" "${sha256}" || return $?
    chmod 0755 "${ENVOY_BIN}" || return 30
}

# ---------- 分层获取(build 模式) ----------
# 按优先级依次尝试,每层获取后都以 read_envoy_version 强校验 == SOFTWARE_VERSION,不匹配即降级到下一层:
#   1. 上游官方二进制:GitHub release 资产 envoy-<version>-linux-<arch>(官方 aarch64/x86_64 tarball)。
#   2. 三方仓库已打包二进制/包:envoy 不在 ubuntu/debian 发行版仓库内(config/distribution.yaml:
#      apt=null、conda=null),官方第三方渠道仅为容器镜像 envoyproxy/envoy;build 模式运行在
#      官方基础镜像内、无嵌套 docker/podman,镜像渠道不可用 → 本层不可行,等效交付物即第 1 层的官方 tarball。
#   3. 源码编译:envoy 上游需 Bazel 全量工具链构建,超出 build 模式基础镜像能力 → 本层不可行。
acquire_envoy_upstream_binary() {
    local actual
    install_release_artifacts || return $?
    actual="$(read_envoy_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "upstream envoy binary is '${actual:-unknown}', != requested ${SOFTWARE_VERSION}"
        return 1
    }
    return 0
}

acquire_envoy_from_repo() {
    log "repository layer infeasible: no distro envoy package (apt=null) and the official channel is a container image (no nested containers in build mode)"
    return 1
}

build_envoy_from_source() {
    log "source layer infeasible in the build-mode base image: envoy requires a full Bazel toolchain"
    return 1
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_runtime_commands || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只校验可执行文件与版本。
        [[ -x "${ENVOY_BIN}" ]] || {
            log "ERROR: envoy is not present in the software image (${ENVOY_BIN})"
            return 40
        }
    else
        [[ ! -e "${INSTALL_DIR}" ]] || {
            log "ERROR: install directory is not clean: ${INSTALL_DIR}"
            return 20
        }
        acquire_envoy_upstream_binary || {
            log "envoy upstream-binary layer failed; trying the repository layer"
            acquire_envoy_from_repo || {
                log "envoy repository layer failed; trying the source layer"
                build_envoy_from_source || {
                    log "ERROR: every envoy acquisition layer failed for ${SOFTWARE_VERSION}"
                    return 40
                }
            }
        }
    fi

    actual="$(read_envoy_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: envoy ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "envoy ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare Envoy for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Envoy version (default: ${SOFTWARE_VERSION})
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