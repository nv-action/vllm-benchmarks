#!/usr/bin/env bash
# Ceph 构建脚本:环境采集 + 准备被测软件(分层获取或校验镜像内已安装版本)。
#   FUNC_MODE=prebuilt - 校验镜像内 ceph 版本,不安装
#   FUNC_MODE=build    - 按优先级分层获取:
#                        1) 上游官方二进制 tarball  2) 厂商/发行版仓库中版本一致的包  3) 源码编译
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

run_privileged() {
    if [[ "$(id -u)" -eq 0 ]]; then
        "$@"
    else
        sudo -n "$@"
    fi
}

# 配置官方 Ceph 仓库并安装客户端与核心守护进程(仅覆盖不含该组件的 core 组件)。
install_ceph_from_repo() {
    local family codename key_tmp
    family="$(os_family)"
    case "${family}" in
        debian)
            codename="$(sed -n 's/^VERSION_CODENAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"')"
            [[ -n "${codename}" ]] || {
                log "ERROR: cannot detect the Debian/Ubuntu codename for the Ceph repo"
                return 30
            }
            key_tmp="${TMPDIR}/ceph-release.asc"
            log "configuring official Ceph repo (${CEPH_RELEASE}) for ${codename}"
            if ! curl -fsSL --retry 3 --connect-timeout 30 -o "${key_tmp}" \
                "${CEPH_REPO_BASE}/keys/release.asc"; then
                log "ERROR: failed to download the Ceph release key"
                return 30
            fi
            gpg --dearmor <"${key_tmp}" >"${TMPDIR}/ceph.gpg" || {
                log "ERROR: failed to dearmor the Ceph release key"
                return 30
            }
            run_privileged install -m 0644 "${TMPDIR}/ceph.gpg" /usr/share/keyrings/ceph.gpg
            printf 'deb [signed-by=/usr/share/keyrings/ceph.gpg] %s/debian-%s/ %s main\n' \
                "${CEPH_REPO_BASE}" "${CEPH_RELEASE}" "${codename}" >"${TMPDIR}/ceph.list"
            run_privileged install -m 0644 "${TMPDIR}/ceph.list" /etc/apt/sources.list.d/ceph.list
            apt_install ceph-common ceph-mon ceph-osd ceph-mgr
            ;;
        rhel)
            # openeuler/rhel 使用发行版仓库提供的 ceph 组件。
            dnf_install ceph-common ceph-mon ceph-osd ceph-mgr
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# ---------- 分层获取(build 模式) ----------
# 优先级:1) 上游官方二进制 tarball  2) 厂商/发行版仓库中版本一致的包  3) 源码编译。
# 每层获取后都用 ceph --version 校验版本 == SOFTWARE_VERSION;
# 不匹配或获取失败即降级到下一层,三层都不可用才让 install 失败。

# 定位二进制并校验版本是否等于请求版本(层间硬门槛)。
ceph_layer_ready() {
    resolve_ceph_binaries >/dev/null 2>&1 || return 1
    [[ "$(ceph_version_of "${CEPH_BIN}")" == "${SOFTWARE_VERSION}" ]]
}

# 层 1:上游官方二进制 tarball。
#   Ceph 官方不发布可直接使用的二进制 tarball:download.ceph.com/tarballs/ 只有源码 tarball,
#   官方二进制以 deb/rpm 包仓库(download.ceph.com 的 debian-*/rpm-*)与容器镜像
#   (quay.io/ceph/ceph)分发。build 模式运行在普通容器内、没有嵌套 docker/podman,
#   官方镜像无法直接使用,故二进制获取落到层 2 的等效仓库包。本层无可用渠道。
install_ceph_from_upstream_binary() {
    log "build layer 1/3: ceph upstream publishes no binary tarball (only source tarball + deb/rpm repos + images); layer unavailable"
    return 1
}

# 层 3:源码编译(最后手段)。下载官方源码 tarball,装构建依赖后用 CMake 编译安装。
install_ceph_from_source() {
    local src="${PERF_WORK_DIR}/ceph-src" tarball="" suffix
    command -v cmake >/dev/null 2>&1 || {
        log "layer 3: cmake is required to build ceph from source"
        return 30
    }
    rm -rf -- "${src}"
    mkdir -p "${src}" || return 30
    # 不同版本的上游 tarball 后缀不一致(如 16.2.8 只有 .tar.gz),逐个尝试。
    for suffix in .tar.gz .tar.bz2; do
        tarball="${PERF_WORK_DIR}/ceph-${SOFTWARE_VERSION}${suffix}"
        log "layer 3: downloading ceph ${SOFTWARE_VERSION} source tarball (${suffix})"
        if curl -fsSL --retry 3 --connect-timeout 30 -o "${tarball}" \
            "${CEPH_TARBALL_URL}/ceph-${SOFTWARE_VERSION}${suffix}"; then
            break
        fi
        tarball=""
    done
    [[ -n "${tarball}" ]] || {
        log "layer 3: failed to download the ceph source tarball"
        return 30
    }
    if ! tar -xf "${tarball}" -C "${src}" --strip-components=1; then
        log "layer 3: failed to extract the ceph source tarball"
        return 30
    fi
    rm -f "${tarball}"
    # 上游自带脚本安装构建依赖;随后 out-of-tree 构建并安装。
    if [[ -x "${src}/install-deps.sh" ]]; then
        (cd "${src}" && run_privileged ./install-deps.sh) || {
            log "layer 3: failed to install the ceph build dependencies"
            return 30
        }
    fi
    if ! (cd "${src}" && ./do_cmake.sh -DCMAKE_BUILD_TYPE=RelWithDebInfo -DWITH_TESTS=OFF); then
        log "layer 3: ceph cmake configuration failed"
        return 30
    fi
    if ! (cd "${src}/build" && make -j"$(nproc)" ceph-mon ceph-osd ceph-mgr ceph-common); then
        log "layer 3: ceph source build failed"
        return 30
    fi
    (cd "${src}/build" && run_privileged make install) || {
        log "layer 3: ceph source install failed"
        return 30
    }
}

# build 模式分层安装:任一层通过 ceph --version 版本校验即停止。
install_ceph_build() {
    if install_ceph_from_upstream_binary && ceph_layer_ready; then
        log "build layer 1/3 satisfied ceph ${SOFTWARE_VERSION} (upstream binary)"
        return 0
    fi
    log "build layer 1/3 did not provide ceph ${SOFTWARE_VERSION}; degrading"
    if install_ceph_from_repo && ceph_layer_ready; then
        log "build layer 2/3 satisfied ceph ${SOFTWARE_VERSION} (official/distro repo package)"
        return 0
    fi
    log "build layer 2/3 did not provide ceph ${SOFTWARE_VERSION}; degrading"
    if install_ceph_from_source && ceph_layer_ready; then
        log "build layer 3/3 satisfied ceph ${SOFTWARE_VERSION} (source build)"
        return 0
    fi
    log "ERROR: no build layer provided ceph ${SOFTWARE_VERSION}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本,不做任何安装。
        resolve_ceph_binaries || {
            log "ERROR: ceph is not present in the software image"
            return 40
        }
        require_test_commands || return $?
    else
        if [[ -n "${CEPH_ROOTFS}" ]]; then
            # Ubuntu 26.04 没有该版本的发行版 ceph 包:框架已把预构建镜像的根文件系统
            # 导出到 CEPH_ROOTFS,软件本体就在里面,跳过所有安装层,只定位并校验版本。
            log "build mode reuses the exported prebuilt image rootfs at ${CEPH_ROOTFS}"
            resolve_ceph_binaries || {
                log "ERROR: ceph is not present in the exported image rootfs"
                return 40
            }
            require_test_commands || return $?
        else
            require_build_commands || return $?
            install_ceph_build || return $?
        fi
    fi

    actual="$(ceph_version_of "${CEPH_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: ceph ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "ceph ${actual} ready at ${CEPH_BIN} (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare Ceph for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Ceph version (default: ${SOFTWARE_VERSION})
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