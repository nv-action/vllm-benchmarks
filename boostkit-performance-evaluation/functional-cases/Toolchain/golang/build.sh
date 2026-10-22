#!/usr/bin/env bash
# golang 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 go 版本,不构建
#   FUNC_MODE=build    - 装依赖 + 分层获取并校验版本:
#                        层1 上游官方二进制(go.dev/dl)→ 层2 三方仓库包 → 层3 源码编译
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

# 官方发布包的 SHA-256(按"版本:架构"声明);新增版本必须在此登记校验值。
go_archive_sha256() {
    case "${SOFTWARE_VERSION}:${GO_ARCH}" in
        1.26.7:amd64)
            printf '%s\n' "ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca"
            ;;
        1.26.7:arm64)
            printf '%s\n' "5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37"
            ;;
        1.27.0:amd64)
            printf '%s\n' "675c26c449cbb18fc24b74650de1eabbae6e16f64326fd85a283fb3b58280685"
            ;;
        1.27.0:arm64)
            printf '%s\n' "51798d2c42d0e1c6ed7fd9f48728b4193abac9e8aad6dbac2fe96a81f5909bda"
            ;;
        1.27.1:amd64)
            printf '%s\n' "63d339f0da5ab53635a56f2490a7984dfe12dfcff22ad749f63edaf590168445"
            ;;
        1.27.1:arm64)
            printf '%s\n' "3450b45a3f9ee8568792736a5c5e70a1f2e9b36c35a8f74958c03e51d7d92bec"
            ;;
        *)
            return 1
            ;;
    esac
}

# 下载官方 Go 二进制包并校验 SHA-256。
install_go_archive() {
    local expected actual filename archive
    expected="$(go_archive_sha256 || true)"
    [[ -n "${expected}" ]] || {
        log "ERROR: no verified Go release is declared for ${SOFTWARE_VERSION} on ${GO_ARCH}"
        return 30
    }
    if [[ -e "${GO_INSTALL_DIR}" ]]; then
        log "ERROR: installation directory is not clean: ${GO_INSTALL_DIR}"
        return 20
    fi
    filename="go${SOFTWARE_VERSION}.linux-${GO_ARCH}.tar.gz"
    archive="${PERF_WORK_DIR}/${filename}"
    log "downloading official Go archive ${filename}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${GO_RELEASE_URL}/${filename}" || {
        log "ERROR: failed to download ${filename}"
        return 30
    }
    actual="$(sha256sum "${archive}" | awk '{print $1}')"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: Go archive checksum mismatch: ${filename}"
        return 30
    }
    mkdir -p "${GO_INSTALL_DIR}" || return 30
    tar -xzf "${archive}" -C "${GO_INSTALL_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract ${filename}"
        return 30
    }
    rm -f "${archive}"
    log "installed official precompiled Go ${SOFTWARE_VERSION} for ${GO_ARCH}"
}

# ---------- 分层获取(build 模式) ----------
# 按优先级(高→低)依次尝试,每一层获取后都必须用 `go version` 校验版本 == SOFTWARE_VERSION,
# 不匹配或获取失败即降级到下一层;三层都不可行才让 install 失败。
# 渠道依据 config/distribution.yaml 的 golang 条目:primary=upstream-tarball(go.dev/dl),
# channels=conda-forge/go、apt golang-go、docker library/golang。

# 层1:上游官方二进制 tarball(go.dev/dl 的 go<VER>.linux-<arch>.tar.gz,SHA-256 校验)。
install_go_layer_upstream_binary() {
    install_go_archive
}

# 层2:三方仓库中与请求版本一致的 Go 包。
#   build 模式无嵌套容器 → library/golang 镜像不可用;基础镜像也未内置 conda →
#   改用等效的发行版仓库包(apt golang-go / golang-<minor>-go)。
install_go_layer_repo_package() {
    local package candidate version
    for package in "golang-${SOFTWARE_VERSION%.*}-go" "golang-go"; do
        if ! apt_install "${package}" >/dev/null 2>&1; then
            log "layer2: distro package ${package} is unavailable"
            continue
        fi
        candidate="$(command -v go 2>/dev/null || true)"
        [[ -n "${candidate}" ]] || continue
        version="$(go_version_of "${candidate}")"
        if [[ "${version}" == "${SOFTWARE_VERSION}" ]]; then
            printf 'GO_BIN=%s\n' "${candidate}" >"${LAYERED_TOOL_FILE}"
            log "layer2: adopted distro Go package ${package} (${candidate})"
            return 0
        fi
        log "layer2: distro package ${package} provides Go ${version:-unknown}, not ${SOFTWARE_VERSION}"
    done
    return 1
}

# 层3:从官方源码 tarball 编译(最后手段,需要有可用的引导 Go)。
install_go_layer_source_build() {
    local bootstrap bootstrap_root archive
    bootstrap="$(command -v go 2>/dev/null || true)"
    [[ -n "${bootstrap}" ]] || {
        log "layer3: no bootstrap Go is available for a source build"
        return 1
    }
    bootstrap_root="$(cd "$(dirname "${bootstrap}")/.." && pwd)"
    archive="${PERF_WORK_DIR}/go${SOFTWARE_VERSION}.src.tar.gz"
    log "layer3: downloading official Go source go${SOFTWARE_VERSION}.src.tar.gz"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${GO_RELEASE_URL}/go${SOFTWARE_VERSION}.src.tar.gz" || {
        log "layer3: failed to download the Go source tarball"
        return 1
    }
    rm -rf -- "${GO_INSTALL_DIR}"
    mkdir -p "${GO_INSTALL_DIR}" || return 1
    tar -xzf "${archive}" -C "${GO_INSTALL_DIR}" --strip-components=1 || {
        log "layer3: failed to extract the Go source tarball"
        return 1
    }
    rm -f "${archive}"
    log "layer3: building Go ${SOFTWARE_VERSION} from source (bootstrap ${bootstrap_root})"
    (
        cd "${GO_INSTALL_DIR}/src"
        GOROOT_BOOTSTRAP="${bootstrap_root}" GOROOT_FINAL="${GO_INSTALL_DIR}" ./make.bash
    ) || {
        log "layer3: Go source build failed"
        return 1
    }
    [[ -x "${GO_BIN}" ]] || {
        log "layer3: source build did not produce ${GO_BIN}"
        return 1
    }
    return 0
}

# 分层获取入口:依次尝试三层,任一层成功即返回 0。
install_go_layered() {
    if install_go_layer_upstream_binary; then
        log "acquired Go via layer 1 (upstream official binary)"
        return 0
    fi
    log "layer 1 (upstream official binary) unavailable; falling back to layer 2"
    if install_go_layer_repo_package; then
        log "acquired Go via layer 2 (third-party repo package)"
        return 0
    fi
    log "layer 2 (third-party repo package) unavailable; falling back to layer 3"
    if install_go_layer_source_build; then
        log "acquired Go via layer 3 (source build)"
        return 0
    fi
    log "ERROR: no Go ${SOFTWARE_VERSION} acquisition layer succeeded"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    configure_go_environment

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -x "${GO_BIN}" ]] || {
            log "ERROR: go is not present in the software image"
            return 40
        }
    else
        # 当场安装:装依赖 + 分层获取(上游二进制 → 三方仓库包 → 源码编译)。
        require_build_commands || return $?
        install_go_layered || return $?
        # layer2 可能改选发行版包的 go,重新解析工具路径后再统一校验版本。
        configure_runtime_paths || return $?
    fi

    actual="$(go_version_of "${GO_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: go ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "golang ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare golang for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Go version (default: ${SOFTWARE_VERSION})
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