#!/usr/bin/env bash
# Kubernetes 构建脚本:环境采集 + 准备被测软件(下载或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 kubectl 客户端版本
#   FUNC_MODE=build    - 分层获取:dl.k8s.io 官方二进制(优先)→ pkgs.k8s.io 版本化包 → 源码编译(逐层按版本强校验降级)
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

# 优先使用 runner 离线目录中的官方 kubectl,否则下载二进制并用同名 .sha256 校验。
install_kubectl() {
    local arch url sha_url offline expected actual
    arch="$(kubectl_arch "${EXPECTED_ARCH}")"
    offline="${KUBECTL_OFFLINE_DIR}/kubectl-v${SOFTWARE_VERSION}-linux-${arch}"
    mkdir -p "${INSTALL_DIR}" || return 30
    if [[ -f "${offline}" ]]; then
        log "using local kubectl asset ${offline}"
        install -m 0755 "${offline}" "${KUBECTL_BIN}" || return 30
        return 0
    fi
    url="${KUBECTL_DOWNLOAD_BASE}/v${SOFTWARE_VERSION}/bin/linux/${arch}/kubectl"
    log "downloading official kubectl ${SOFTWARE_VERSION} for linux/${arch}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${KUBECTL_BIN}" "${url}" || {
        log "ERROR: download failed: ${url}"
        return 30
    }
    sha_url="${url}.sha256"
    expected="$(curl -fsSL --retry 3 --connect-timeout 30 "${sha_url}" | awk '{print $1}')" || {
        log "ERROR: could not fetch checksum: ${sha_url}"
        return 30
    }
    actual="$(sha256sum "${KUBECTL_BIN}" | awk '{print $1}')"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: kubectl checksum mismatch"
        return 30
    }
    chmod 0755 "${KUBECTL_BIN}" || return 30
}

# ---------- 分层获取(build 模式) ----------
# 按优先级依次尝试,每层都以 read_kubectl_version 强校验 == SOFTWARE_VERSION,不匹配即降级到下一层:
#   1. 上游官方二进制:dl.k8s.io/release/v<version>/bin/linux/<arch>/kubectl(+ 同名 .sha256)。
#   2. 三方仓库已打包二进制/包:pkgs.k8s.io 版本化 apt 仓库(config/distribution.yaml:
#      apt=pkgs.k8s.io),安装版本严格等于请求版本的 kubectl 包(amd64/arm64 均有)。
#   3. 源码编译:kubernetes 上游源码需完整 Go 工具链与整仓克隆构建,超出 build 模式基础镜像能力 → 本层不可行。
acquire_kubernetes_upstream_binary() {
    local actual
    install_kubectl || return $?
    actual="$(read_kubectl_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "official kubectl binary is '${actual:-unknown}', != requested ${SOFTWARE_VERSION}"
        return 1
    }
    return 0
}

# 从 pkgs.k8s.io 安装与请求版本严格相等的 kubectl 包;找不到精确版本或安装失败则返回非零以触发降级。
acquire_kubernetes_from_repo() {
    local minor repo key_url keyring list_file candidate actual
    [[ -d /etc/apt ]] || { log "apt is unavailable; skipping the repository layer"; return 1; }
    command -v gpg >/dev/null 2>&1 || {
        log "gpg is unavailable; cannot configure the pkgs.k8s.io repository"
        return 1
    }
    minor="$(printf '%s' "${SOFTWARE_VERSION}" | sed -n 's/^\([0-9][0-9]*\.[0-9][0-9]*\).*/\1/p')"
    [[ -n "${minor}" ]] || return 1
    repo="${KUBERNETES_APT_REPO_BASE}/core:/stable:/v${minor}/deb"
    key_url="${repo}/Release.key"
    keyring="/etc/apt/keyrings/kubernetes-func.gpg"
    list_file="/etc/apt/sources.list.d/kubernetes-func.list"
    log "configuring the pkgs.k8s.io apt repository for v${minor}"
    if [[ "$(id -u)" -eq 0 ]]; then
        mkdir -p /etc/apt/keyrings || return 1
        curl -fsSL --retry 3 --connect-timeout 30 "${key_url}" | gpg --dearmor -o "${keyring}" || return 1
        printf 'deb [signed-by=%s] %s/ /\n' "${keyring}" "${repo}" >"${list_file}" || return 1
    else
        sudo -n mkdir -p /etc/apt/keyrings || return 1
        curl -fsSL --retry 3 --connect-timeout 30 "${key_url}" | sudo -n gpg --dearmor -o "${keyring}" || return 1
        printf 'deb [signed-by=%s] %s/ /\n' "${keyring}" "${repo}" | sudo -n tee "${list_file}" >/dev/null || return 1
    fi
    apt_get update || { log "apt-get update failed for the pkgs.k8s.io repository"; return 1; }
    candidate="$(apt-cache madison kubectl 2>/dev/null \
        | awk -v prefix="${SOFTWARE_VERSION}-" 'index($3, prefix) == 1 { print $3; exit }')"
    [[ -n "${candidate}" ]] || {
        log "pkgs.k8s.io has no kubectl package matching ${SOFTWARE_VERSION}"
        return 1
    }
    log "installing the distro kubectl package ${candidate} from pkgs.k8s.io"
    apt_get install -y "kubectl=${candidate}" || return 1
    local packaged
    packaged="$(command -v kubectl 2>/dev/null || true)"
    [[ -n "${packaged}" ]] || { log "the packaged kubectl is not on PATH"; return 1; }
    # 落到 build 模式约定的 KUBECTL_BIN(INSTALL_DIR/kubectl),与 check 阶段保持同一路径。
    mkdir -p "${INSTALL_DIR}" || return 1
    install -m 0755 "${packaged}" "${KUBECTL_BIN}" || return 1
    actual="$(read_kubectl_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "packaged kubectl is '${actual:-unknown}', != requested ${SOFTWARE_VERSION}"
        return 1
    }
    return 0
}

build_kubernetes_from_source() {
    log "source layer infeasible in the build-mode base image: kubernetes requires the full Go toolchain and repository build"
    return 1
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_runtime_commands || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只校验可执行文件与版本。
        [[ -x "${KUBECTL_BIN}" ]] || {
            log "ERROR: kubectl is not present in the software image (${KUBECTL_BIN})"
            return 40
        }
    else
        [[ ! -e "${INSTALL_DIR}" ]] || {
            log "ERROR: install directory is not clean: ${INSTALL_DIR}"
            return 20
        }
        acquire_kubernetes_upstream_binary || {
            log "kubernetes upstream-binary layer failed; trying the repository layer"
            acquire_kubernetes_from_repo || {
                log "kubernetes repository layer failed; trying the source layer"
                build_kubernetes_from_source || {
                    log "ERROR: every kubernetes acquisition layer failed for ${SOFTWARE_VERSION}"
                    return 40
                }
            }
        }
    fi

    actual="$(read_kubectl_version || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: kubectl client ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "kubectl ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare kubectl for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Kubernetes release version (default: ${SOFTWARE_VERSION})
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