#!/usr/bin/env bash
# glibc 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 glibc 版本,不构建
#   FUNC_MODE=build    - 装依赖 + 分层获取并校验版本:
#                        层1 上游官方二进制(GNU 仅源码,不可用)→ 层2 三方仓库包(系统 libc6)→ 层3 源码编译
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

# 官方发布包的 SHA-256(按版本声明);新增版本必须在此登记校验值。
glibc_source_sha256() {
    case "${SOFTWARE_VERSION}" in
        2.31)
            printf '%s\n' "9246fe44f68feeec8c666bb87973d590ce0137cca145df014c72ec95be9ffd17"
            ;;
        2.34)
            printf '%s\n' "44d26a1fe20b8853a48f470ead01e4279e869ac149b195dda4e44a195d981ab2"
            ;;
        2.43)
            printf '%s\n' "d9c86c6b5dbddb43a3e08270c5844fc5177d19442cf5b8df4be7c07cd5fa3831"
            ;;
        *)
            return 1
            ;;
    esac
}

# 下载官方 glibc 源码 tarball、校验 SHA-256 并解压到私有源码目录。
prepare_glibc_source() {
    local expected actual tarball source_root
    expected="$(glibc_source_sha256 || true)"
    [[ -n "${expected}" ]] || {
        log "ERROR: no verified glibc release is declared for ${SOFTWARE_VERSION}"
        return 30
    }
    if [[ -e "${SRC_DIR}" ]]; then
        log "ERROR: source directory is not clean: ${SRC_DIR}"
        return 30
    fi
    tarball="${PERF_WORK_DIR}/glibc-${SOFTWARE_VERSION}.tar.xz"
    log "downloading official glibc ${SOFTWARE_VERSION} release tarball from ${GLIBC_SOURCE_URL}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${tarball}" \
        "${GLIBC_SOURCE_URL}/glibc-${SOFTWARE_VERSION}.tar.xz" || {
        log "ERROR: failed to download glibc-${SOFTWARE_VERSION}.tar.xz"
        return 30
    }
    actual="$(sha256sum "${tarball}" | awk '{print $1}')"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: glibc tarball checksum mismatch"
        return 30
    }
    tar -xJf "${tarball}" -C "${PERF_WORK_DIR}" || {
        log "ERROR: failed to extract the glibc tarball"
        return 30
    }
    rm -f "${tarball}"
    source_root="${PERF_WORK_DIR}/glibc-${SOFTWARE_VERSION}"
    [[ -f "${source_root}/configure" ]] || {
        log "ERROR: tarball did not create glibc-${SOFTWARE_VERSION}/configure"
        return 30
    }
    mv "${source_root}" "${SRC_DIR}" || {
        log "ERROR: failed to move the unpacked source into ${SRC_DIR}"
        return 30
    }
}

# glibc 2.31 使用较新的系统 make 会反复重执行部分目标,构建私有 GNU Make 4.3 规避。
prepare_compatible_make() {
    local archive="${PERF_WORK_DIR}/make-${GLIBC_MAKE_VERSION}.tar.gz"
    local source_dir="${PERF_WORK_DIR}/make-${GLIBC_MAKE_VERSION}-src"
    local actual_sha256

    if [[ -x "${MAKE_INSTALL_DIR}/bin/make" ]]; then
        PATH="${MAKE_INSTALL_DIR}/bin:${PATH}"
        export PATH
        return 0
    fi
    log "downloading GNU Make ${GLIBC_MAKE_VERSION} from ${GLIBC_MAKE_URL}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${GLIBC_MAKE_URL}/make-${GLIBC_MAKE_VERSION}.tar.gz" || {
        log "ERROR: failed to download GNU Make ${GLIBC_MAKE_VERSION}"
        return 30
    }
    actual_sha256="$(sha256sum "${archive}" | awk '{print $1}')"
    [[ "${actual_sha256}" == "${GLIBC_MAKE_SHA256}" ]] || {
        log "ERROR: GNU Make ${GLIBC_MAKE_VERSION} archive checksum mismatch"
        return 30
    }
    mkdir -p "${source_dir}" || return 30
    tar -xzf "${archive}" -C "${source_dir}" --strip-components=1 || {
        log "ERROR: failed to extract GNU Make ${GLIBC_MAKE_VERSION}"
        return 30
    }
    rm -f "${archive}"
    log "building GNU Make ${GLIBC_MAKE_VERSION} in the private work directory"
    (
        cd "${source_dir}"
        ./configure --prefix="${MAKE_INSTALL_DIR}" --disable-nls
        make -j4
        make install
    ) || {
        log "ERROR: failed to build GNU Make ${GLIBC_MAKE_VERSION}"
        return 30
    }
    [[ "$("${MAKE_INSTALL_DIR}/bin/make" --version | head -n 1)" == "GNU Make ${GLIBC_MAKE_VERSION}" ]] || {
        log "ERROR: private GNU Make ${GLIBC_MAKE_VERSION} is unavailable"
        return 30
    }
    PATH="${MAKE_INSTALL_DIR}/bin:${PATH}"
    export PATH
}

# 独立构建并安装到私有前缀(不更新系统动态链接器缓存)。
build_glibc_from_source() {
    local actual_version version_output

    prepare_glibc_source || return $?
    # 仅 glibc 2.31 需要私有 GNU Make 4.3;2.43 用系统 make 即可,不必多编译一次。
    if [[ "${SOFTWARE_VERSION}" == "2.31" ]]; then
        prepare_compatible_make || return $?
    fi

    log "configuring glibc ${SOFTWARE_VERSION} (out-of-tree build)"
    mkdir -p "${BUILD_DIR}" || return 40
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        "${SRC_DIR}/configure" \
            --prefix="${INSTALL_DIR}" \
            --disable-werror \
            --without-selinux \
            CC=gcc CFLAGS=-O3
    ) || {
        log "ERROR: glibc configure failed"
        return 40
    }
    log "building glibc ${SOFTWARE_VERSION} with make -j$(nproc)"
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        # 运行器较新的 libstdc++ 需要 glibc 2.31 缺失的符号;用 glibc 自带 C 版构建辅助程序。
        make -j"$(nproc)" CXX=
    ) || {
        log "ERROR: glibc make build failed"
        return 40
    }
    log "installing glibc ${SOFTWARE_VERSION} into the isolated prefix ${INSTALL_DIR}"
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        # cross-compiling=yes 抑制安装期对系统 ldconfig 缓存的更新。
        make install cross-compiling=yes CXX=
    ) || {
        log "ERROR: glibc installation into ${INSTALL_DIR} failed"
        return 40
    }
    [[ -x "${LDCONFIG_BIN}" ]] || {
        log "ERROR: built ldconfig is missing: ${LDCONFIG_BIN}"
        return 40
    }
    [[ -n "$(loader_under_prefix "${INSTALL_DIR}")" ]] || {
        log "ERROR: installed glibc dynamic loader is missing under ${INSTALL_DIR}/lib"
        return 40
    }
    version_output="$("${LDCONFIG_BIN}" --version 2>/dev/null | head -n 1)" || {
        log "ERROR: built ldconfig cannot report its version"
        return 40
    }
    actual_version="$(normalize_version "$(printf '%s\n' "${version_output}" | awk '{print $NF}')")"
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: built glibc reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    }
    log "glibc ${SOFTWARE_VERSION} build is ready at ${BUILD_DIR}"
}

# ---------- 分层获取(build 模式) ----------
# 按优先级(高→低)依次尝试,每一层获取后都必须校验版本 == SOFTWARE_VERSION
# (优先用 ldconfig,否则用动态加载器的 `--version`),不匹配或获取失败即降级到下一层;
# 三层都不可行才让 install 失败。
# 渠道依据 config/distribution.yaml 的 glibc 条目:primary=upstream-release(ftp.gnu.org),
# channels=apt libc6-dev、spack glibc(pypi/conda/docker 均为 null)。GNU 官方只发布源码。

# 层1:上游官方二进制 tarball。ftp.gnu.org 只发布源码,无官方预编译二进制 → 结构性不可用。
install_glibc_layer_upstream_binary() {
    log "layer1: ftp.gnu.org publishes source only (no official binary); skipping to layer 2"
    return 1
}

# 层2:三方仓库中与请求版本一致的 glibc 包(发行版仓库的 libc6/libc-bin）。
#   build 模式无嵌套容器 → distribution.yaml 里 glibc 的 docker 渠道本就为 null;
#   直接采用基础镜像经 libc6 包提供的系统 glibc(即 glibc-<os> 发行版包)。
install_glibc_layer_repo_package() {
    local loader version ldconfig_bin
    apt_install libc6 libc-bin >/dev/null 2>&1 || true
    loader="$(system_glibc_loader || true)"
    [[ -n "${loader}" ]] || {
        log "layer2: the distro-provided glibc loader was not found"
        return 1
    }
    version="$(loader_version_of "${loader}")"
    if [[ "${version}" != "${SOFTWARE_VERSION}" ]]; then
        log "layer2: distro package libc6 provides glibc ${version:-unknown}, not ${SOFTWARE_VERSION}"
        return 1
    fi
    ldconfig_bin="$(command -v ldconfig 2>/dev/null || true)"
    {
        printf 'GLIBC_LOADER=%s\n' "${loader}"
        printf 'GLIBC_LIB_DIR=%s\n' "$(loader_lib_dir "${loader}")"
        if [[ -n "${ldconfig_bin}" ]]; then
            printf 'LDCONFIG_BIN=%s\n' "${ldconfig_bin}"
        fi
    } >"${LAYERED_TOOL_FILE}"
    log "layer2: adopted the distro glibc package (loader ${loader})"
    return 0
}

# 层2b:发行版归档仓库里的历史 glibc 二进制包。
# glibc 是系统库,当前发行版仓库只提供自己那一版;但 old-releases 归档里保留了
# 各历史发行版的 libc6/libc-bin .deb(Ubuntu 21.10 impish 即 2.34),
# 解包到私有前缀即可,属于"三方仓库已打包的二进制",无需源码编译。
GLIBC_ARCHIVE_BASE="${GLIBC_ARCHIVE_BASE:-https://old-releases.ubuntu.com/ubuntu/pool/main/g/glibc}"

install_glibc_layer_archive_binary() {
    local deb_arch listing libc6_deb libc_bin_deb deb loader version
    [[ "$(os_family)" == "debian" ]] || {
        log "layer2b: archive binary packages are only wired for debian-family systems"
        return 1
    }
    case "$(normalize_arch "${EXPECTED_ARCH}")" in
        aarch64) deb_arch="arm64" ;;
        x86_64) deb_arch="amd64" ;;
        *)
            log "layer2b: unsupported architecture ${EXPECTED_ARCH}"
            return 1
            ;;
    esac
    command -v dpkg-deb >/dev/null 2>&1 || install_os_packages dpkg || return 1
    listing="${PERF_WORK_DIR}/glibc-archive-index.html"
    mkdir -p "${PERF_WORK_DIR}/debs" "${INSTALL_DIR}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${listing}" "${GLIBC_ARCHIVE_BASE}/" || {
        log "layer2b: cannot list ${GLIBC_ARCHIVE_BASE}"
        return 1
    }
    # 目录里有多个修订版本,用 sort -V 取最高者。
    libc6_deb="$(
        sed -n 's/.*href="\([^"]*\)".*/\1/p' "${listing}" \
            | sed -n "\#^libc6_${SOFTWARE_VERSION}-.*_${deb_arch}\.deb\$#p" \
            | sort -V | tail -n 1
    )"
    libc_bin_deb="$(
        sed -n 's/.*href="\([^"]*\)".*/\1/p' "${listing}" \
            | sed -n "\#^libc-bin_${SOFTWARE_VERSION}-.*_${deb_arch}\.deb\$#p" \
            | sort -V | tail -n 1
    )"
    [[ -n "${libc6_deb}" ]] || {
        log "layer2b: the archive publishes no libc6 ${SOFTWARE_VERSION} package for ${deb_arch}"
        return 1
    }
    for deb in "${libc6_deb}" "${libc_bin_deb}"; do
        [[ -n "${deb}" ]] || continue
        curl -fsSL --retry 3 --connect-timeout 30 -o "${PERF_WORK_DIR}/debs/${deb}" \
            "${GLIBC_ARCHIVE_BASE}/${deb}" || {
            log "layer2b: failed to download ${deb}"
            rm -rf -- "${INSTALL_DIR}"
            return 1
        }
        dpkg-deb -x "${PERF_WORK_DIR}/debs/${deb}" "${INSTALL_DIR}" || {
            log "layer2b: failed to unpack ${deb}"
            rm -rf -- "${INSTALL_DIR}"
            return 1
        }
    done
    loader="$(loader_under_prefix "${INSTALL_DIR}" || true)"
    [[ -n "${loader}" ]] || {
        log "layer2b: the unpacked glibc provides no dynamic loader"
        rm -rf -- "${INSTALL_DIR}"
        return 1
    }
    version="$(loader_version_of "${loader}")"
    if [[ "${version}" != "${SOFTWARE_VERSION}" ]]; then
        log "layer2b: archive package provides glibc ${version:-unknown}, not ${SOFTWARE_VERSION}"
        rm -rf -- "${INSTALL_DIR}"
        return 1
    fi
    {
        printf 'GLIBC_LOADER=%s\n' "${loader}"
        printf 'GLIBC_LIB_DIR=%s\n' "$(loader_lib_dir "${loader}")"
        if [[ -x "${INSTALL_DIR}/usr/bin/getconf" ]]; then
            printf 'GLIBC_GETCONF=%s\n' "${INSTALL_DIR}/usr/bin/getconf"
        fi
    } >"${LAYERED_TOOL_FILE}"
    log "layer2b: adopted archived distro glibc ${version} (loader ${loader})"
    return 0
}

# 层3:从官方源码 tarball 独立构建(最后手段;GNU 官方仅分发源码,三方仓库版本多滞后)。
install_glibc_layer_source_build() {
    build_glibc_from_source
}

# 分层获取入口:依次尝试四层,任一层成功即返回 0。
install_glibc_layered() {
    if install_glibc_layer_upstream_binary; then
        log "acquired glibc via layer 1 (upstream official binary)"
        return 0
    fi
    log "layer 1 (upstream official binary) unavailable; falling back to layer 2"
    if install_glibc_layer_repo_package; then
        log "acquired glibc via layer 2 (third-party repo package)"
        return 0
    fi
    log "layer 2 (third-party repo package) unavailable; falling back to layer 2b"
    if install_glibc_layer_archive_binary; then
        log "acquired glibc via layer 2b (archived distro binary package)"
        return 0
    fi
    log "layer 2b (archived distro binary package) unavailable; falling back to layer 3"
    if install_glibc_layer_source_build; then
        log "acquired glibc via layer 3 (source build)"
        return 0
    fi
    log "ERROR: no glibc ${SOFTWARE_VERSION} acquisition layer succeeded"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本,并确保存在编译功能程序的 C 编译器。
        require_test_compiler || return $?
    else
        # 当场构建:装依赖 + 分层获取(官方二进制→三方仓库包→官方源码构建到私有前缀)。
        require_build_commands || return $?
        [[ ! -e "${BUILD_DIR}" && ! -e "${INSTALL_DIR}" ]] || {
            log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
            return 20
        }
        install_glibc_layered || return $?
        # layer2 可能改选系统 glibc,重新解析加载器/ldconfig 后再统一校验版本。
        configure_runtime_paths || return $?
    fi

    actual="$(glibc_version)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: glibc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "glibc ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare glibc for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    glibc version (default: ${SOFTWARE_VERSION})
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