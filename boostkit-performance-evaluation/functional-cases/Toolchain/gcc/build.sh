#!/usr/bin/env bash
# gcc 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 gcc 版本,不构建
#   FUNC_MODE=build    - 装依赖 + 分层获取并校验版本:
#                        层1 上游官方二进制(GNU 仅源码,不可用)→ 层2 三方仓库包 →
#                        层2b 发行版归档二进制包 → 层3 源码编译
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
gcc_source_sha256() {
    case "${SOFTWARE_VERSION}" in
        14.4.0)
            printf '%s\n' "752b6f567beac83159c77a7680b1316bdd784738bff9a9d070112c09da90f6d9"
            ;;
        16.2.0)
            printf '%s\n' "e6738e29597f733270731aa90600f37ffdc045079dfc27ec7e8192cc81085c3e"
            ;;
        *)
            return 1
            ;;
    esac
}

# 下载官方 GCC 源码 tarball、校验 SHA-256 并解压到私有源码目录。
prepare_gcc_source() {
    local expected actual tarball source_root
    expected="$(gcc_source_sha256 || true)"
    [[ -n "${expected}" ]] || {
        log "ERROR: no verified GCC release is declared for ${SOFTWARE_VERSION}"
        return 30
    }
    if [[ -e "${SRC_DIR}" ]]; then
        log "ERROR: source directory is not clean: ${SRC_DIR}"
        return 20
    fi
    tarball="${PERF_WORK_DIR}/gcc-${SOFTWARE_VERSION}.tar.xz"
    log "downloading official gcc-${SOFTWARE_VERSION}.tar.xz from ${GCC_SOURCE_BASE}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${tarball}" \
        "${GCC_SOURCE_BASE}/gcc-${SOFTWARE_VERSION}/gcc-${SOFTWARE_VERSION}.tar.xz" || {
        log "ERROR: failed to download gcc-${SOFTWARE_VERSION}.tar.xz"
        return 30
    }
    actual="$(sha256sum "${tarball}" | awk '{print $1}')"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: GCC tarball checksum mismatch"
        return 30
    }
    log "extracting GCC source (checksum verified)"
    tar -xJf "${tarball}" -C "${PERF_WORK_DIR}" || {
        log "ERROR: failed to extract the GCC tarball"
        return 30
    }
    rm -f "${tarball}"
    source_root="${PERF_WORK_DIR}/gcc-${SOFTWARE_VERSION}"
    [[ -f "${source_root}/configure" ]] || {
        log "ERROR: tarball did not create the expected GCC source tree"
        return 30
    }
    mv "${source_root}" "${SRC_DIR}" || {
        log "ERROR: failed to place the extracted GCC source tree"
        return 30
    }
}

# 独立构建(C/C++,out-of-tree)并安装到私有前缀。
build_gcc_from_source() {
    local version_output actual_version

    prepare_gcc_source || return $?
    log "configuring GCC ${SOFTWARE_VERSION} (C and C++, out-of-tree build)"
    mkdir -p "${BUILD_DIR}" || return 40
    (
        cd "${BUILD_DIR}"
        "${SRC_DIR}/configure" \
            --prefix="${INSTALL_DIR}" \
            --enable-languages=c,c++ \
            --disable-bootstrap \
            --disable-multilib \
            --disable-nls
    ) || {
        log "ERROR: GCC configure failed"
        return 40
    }
    log "building GCC ${SOFTWARE_VERSION} with make -j$(nproc)"
    (
        cd "${BUILD_DIR}"
        make -j"$(nproc)"
    ) || {
        log "ERROR: GCC build failed"
        return 40
    }
    log "installing GCC ${SOFTWARE_VERSION} into ${INSTALL_DIR}"
    (
        cd "${BUILD_DIR}"
        make install
    ) || {
        log "ERROR: GCC private installation failed"
        return 40
    }
    [[ -x "${GCC_BIN}" && -x "${GXX_BIN}" ]] || {
        log "ERROR: private GCC C or C++ compiler is missing"
        return 40
    }
    version_output="$("${GCC_BIN}" --version 2>/dev/null | head -n 1)" || {
        log "ERROR: built GCC cannot report its version"
        return 40
    }
    actual_version="$(printf '%s\n' "${version_output}" | awk '{print $NF}')"
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: built GCC reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    }
    log "GCC ${SOFTWARE_VERSION} compiler is ready at ${GCC_BIN}"
}

# ---------- 分层获取(build 模式) ----------
# 按优先级(高→低)依次尝试,每一层获取后都必须用 `gcc --version` 校验版本 == SOFTWARE_VERSION,
# 不匹配或获取失败即降级到下一层;三层都不可行才让 install 失败。
# 渠道依据 config/distribution.yaml 的 gcc 条目:primary=upstream-release(gcc.gnu.org),
# channels=conda-forge/gcc_impl_linux-64、apt gcc、docker library/gcc。
# 注意:GNU 官方只提供源码,无官方预编译二进制。

# 层1:上游官方二进制 tarball。
#   gcc.gnu.org 只发布源码 tarball,无官方预编译二进制 → 该层结构性不可用。
install_gcc_layer_upstream_binary() {
    log "layer1: gcc.gnu.org publishes source only (no official binary); skipping to layer 2"
    return 1
}

# 层2:三方仓库中与请求版本一致的 gcc 包。
#   build 模式无嵌套容器 → library/gcc 镜像不可用;基础镜像也未内置 conda →
#   改用等效的发行版仓库包(apt gcc-<major> / gcc / g++)。
install_gcc_layer_repo_package() {
    local major="${SOFTWARE_VERSION%%.*}" package candidate version
    for package in "gcc-${major}" gcc; do
        if ! apt_install "${package}" g++ >/dev/null 2>&1; then
            log "layer2: distro package ${package} is unavailable"
            continue
        fi
        candidate="$(command -v "gcc-${major}" 2>/dev/null || command -v gcc 2>/dev/null || true)"
        [[ -n "${candidate}" ]] || continue
        version="$(gcc_version_of "${candidate}")"
        if [[ "${version}" == "${SOFTWARE_VERSION}" ]]; then
            printf 'GCC_BIN=%s\nGXX_BIN=%s\n' "${candidate}" "${candidate/gcc/g++}" >"${LAYERED_TOOL_FILE}"
            log "layer2: adopted distro GCC package ${package} (${candidate})"
            return 0
        fi
        log "layer2: distro package ${package} provides GCC ${version:-unknown}, not ${SOFTWARE_VERSION}"
    done
    return 1
}

# 层2b:发行版归档中的预编译 gcc 包。
#   GNU 官方不发 gcc 二进制,但发行版仓库有现成包:Debian sid 已发布 gcc-16 16.2.0 的 arm64 包。
#   把它们解包到私有前缀(不动系统),再用包装脚本固定 -B 指向解包出来的内部目录,
#   避免误用系统里另一份 gcc 的 cc1/crt。
GCC_DEBIAN_POOL="${GCC_DEBIAN_POOL:-https://deb.debian.org/debian/pool/main/g/gcc-16}"

install_gcc_layer_archive_binary() {
    local deb_arch gnu_triple listing prefix bin_dir cc1_dir crt_dir lib_dir cxx_include cxx_arch_include
    local driver_gcc driver_gxx wrapper_gcc wrapper_gxx deb version candidate major
    local gcc_runtime_path
    local -a packages=()
    [[ "$(os_family)" == "debian" ]] || {
        log "layer2b: archive binary packages are only wired for debian-family systems"
        return 1
    }
    case "$(normalize_arch "${EXPECTED_ARCH}")" in
        aarch64) deb_arch="arm64"; gnu_triple="aarch64-linux-gnu" ;;
        x86_64) deb_arch="amd64"; gnu_triple="x86_64-linux-gnu" ;;
        *)
            log "layer2b: unsupported architecture ${EXPECTED_ARCH}"
            return 1
            ;;
    esac
    major="${SOFTWARE_VERSION%%.*}"
    command -v dpkg-deb >/dev/null 2>&1 || install_os_packages dpkg || return 1
    listing="${PERF_WORK_DIR}/gcc-archive-index.html"
    prefix="${PERF_WORK_DIR}/gcc-deb-prefix"
    bin_dir="${PERF_WORK_DIR}/bin"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${listing}" "${GCC_DEBIAN_POOL}/" || {
        log "layer2b: cannot list ${GCC_DEBIAN_POOL}"
        return 1
    }
    # 编译器被拆成多个包:driver(gcc-16)、cc1/cc1plus(多架构包)、crt/libgcc(libgcc-16-dev)、
    # C++ 头文件与链接脚本(libstdc++-16-dev)、C++ 运行库(libstdc++6)。只取这些必要包,
    # 避免把 fortran/ada/rust 等无关前端一起下载。
    while IFS= read -r candidate; do
        [[ -n "${candidate}" ]] && packages+=("${candidate}")
    done < <(
        sed -n 's/.*href="\([^"]*\)".*/\1/p' "${listing}" \
            | grep -E "^(gcc-${major}|gcc-${major}-base|gcc-${major}-${gnu_triple}|cpp-${major}|cpp-${major}-${gnu_triple}|g\\+\\+-${major}|g\\+\\+-${major}-${gnu_triple}|libgcc-${major}-dev|libstdc\\+\\+-${major}-dev|libstdc\\+\\+6|libgcc-s1)_${SOFTWARE_VERSION}-[^_]*_${deb_arch}\\.deb$" \
            | sort -u
    )
    if (( ${#packages[@]} == 0 )); then
        log "layer2b: the archive publishes no gcc ${SOFTWARE_VERSION} package for ${deb_arch}"
        return 1
    fi
    mkdir -p "${prefix}" "${bin_dir}" "${PERF_WORK_DIR}/debs"
    for deb in "${packages[@]}"; do
        curl -fsSL --retry 3 --connect-timeout 30 -o "${PERF_WORK_DIR}/debs/${deb}" \
            "${GCC_DEBIAN_POOL}/${deb}" || {
            log "layer2b: failed to download ${deb}"
            rm -rf -- "${prefix}" "${bin_dir}"
            return 1
        }
        dpkg-deb -x "${PERF_WORK_DIR}/debs/${deb}" "${prefix}" || {
            log "layer2b: failed to unpack ${deb}"
            rm -rf -- "${prefix}" "${bin_dir}"
            return 1
        }
    done
    driver_gcc="${prefix}/usr/bin/gcc-${major}"
    driver_gxx="${prefix}/usr/bin/g++-${major}"
    # cc1/cc1plus 与 crtbegin.o 分处 libexec 与 lib 两个目录,用 -B 分别指过去。
    cc1_dir=""
    for candidate in "${prefix}"/usr/libexec/gcc/*/"${major}" "${prefix}"/usr/lib/gcc/*/"${major}"; do
        [[ -x "${candidate}/cc1" ]] || continue
        cc1_dir="${candidate}"
        break
    done
    crt_dir=""
    for candidate in "${prefix}"/usr/lib/gcc/*/"${major}"; do
        [[ -e "${candidate}/crtbegin.o" ]] || continue
        crt_dir="${candidate}"
        break
    done
    cxx_include="${prefix}/usr/include/c++/${major}"
    cxx_arch_include="${prefix}/usr/include/$(basename "$(dirname "${cc1_dir:-x}")")/c++/${major}"
    # 解包出来的 libstdc++.so.6 在私有前缀里,不在系统默认库路径上;
    # driver/cc1 本身也是 C++ 程序,运行前必须把它加进库搜索路径。
    lib_dir="${prefix}/usr/lib/${gnu_triple}"
    gcc_runtime_path="${lib_dir}:${crt_dir}"
    [[ -x "${driver_gcc}" && -x "${driver_gxx}" && -n "${cc1_dir}" && -n "${crt_dir}" && -d "${cxx_include}" ]] || {
        log "layer2b: the unpacked compiler layout is incomplete"
        rm -rf -- "${prefix}" "${bin_dir}"
        return 1
    }
    version="$(
        LD_LIBRARY_PATH="${gcc_runtime_path}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" \
            gcc_version_of "${driver_gcc}"
    )"
    if [[ "${version}" != "${SOFTWARE_VERSION}" ]]; then
        log "layer2b: archive package provides gcc ${version:-unknown}, not ${SOFTWARE_VERSION}"
        rm -rf -- "${prefix}" "${bin_dir}"
        return 1
    fi
    wrapper_gcc="${bin_dir}/gcc-${SOFTWARE_VERSION}"
    wrapper_gxx="${bin_dir}/g++-${SOFTWARE_VERSION}"
    # 包装脚本同时做两件事:
    #   1) 导出私有前缀的运行库路径,保证 driver/cc1 自身能加载到配套的 libstdc++;
    #   2) -Wl,-rpath 让产出的程序运行期也优先命中这套库,而不是系统自带的旧 libstdc++。
    cat >"${wrapper_gcc}" <<EOF
#!/bin/sh
LD_LIBRARY_PATH="${gcc_runtime_path}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export LD_LIBRARY_PATH
exec "${driver_gcc}" -B"${cc1_dir}/" -B"${crt_dir}/" \\
    -Wl,-rpath,"${gcc_runtime_path}" "\$@"
EOF
    cat >"${wrapper_gxx}" <<EOF
#!/bin/sh
LD_LIBRARY_PATH="${gcc_runtime_path}\${LD_LIBRARY_PATH:+:\${LD_LIBRARY_PATH}}"
export LD_LIBRARY_PATH
exec "${driver_gxx}" -B"${cc1_dir}/" -B"${crt_dir}/" \\
    -isystem "${cxx_include}" -isystem "${cxx_arch_include}" \\
    -Wl,-rpath,"${gcc_runtime_path}" "\$@"
EOF
    chmod +x "${wrapper_gcc}" "${wrapper_gxx}"
    # 解包出来的编译器依赖相对路径重定位,先就地编译并运行一次自检,失败则降级源码构建。
    printf '#include <stdio.h>\nint main(void){printf("gcc-ok\\n");return 0;}\n' \
        >"${PERF_WORK_DIR}/selfcheck.c"
    printf '#include <iostream>\nint main(){std::cout << "gxx-ok" << std::endl;return 0;}\n' \
        >"${PERF_WORK_DIR}/selfcheck.cc"
    if ! "${wrapper_gcc}" -o "${PERF_WORK_DIR}/selfcheck-c" "${PERF_WORK_DIR}/selfcheck.c" >/dev/null 2>&1 ||
        [[ "$("${PERF_WORK_DIR}/selfcheck-c" 2>/dev/null)" != "gcc-ok" ]]; then
        log "layer2b: the unpacked C compiler fails a self-check"
        rm -rf -- "${prefix}" "${bin_dir}"
        return 1
    fi
    if ! "${wrapper_gxx}" -o "${PERF_WORK_DIR}/selfcheck-cxx" "${PERF_WORK_DIR}/selfcheck.cc" >/dev/null 2>&1 ||
        [[ "$("${PERF_WORK_DIR}/selfcheck-cxx" 2>/dev/null)" != "gxx-ok" ]]; then
        log "layer2b: the unpacked C++ compiler fails a self-check"
        rm -rf -- "${prefix}" "${bin_dir}"
        return 1
    fi
    rm -f "${PERF_WORK_DIR}/selfcheck.c" "${PERF_WORK_DIR}/selfcheck.cc" \
        "${PERF_WORK_DIR}/selfcheck-c" "${PERF_WORK_DIR}/selfcheck-cxx"
    printf 'GCC_BIN=%s\nGXX_BIN=%s\n' "${wrapper_gcc}" "${wrapper_gxx}" >"${LAYERED_TOOL_FILE}"
    log "layer2b: adopted archived distro GCC ${version} (${driver_gcc})"
    return 0
}

# 层3:从官方源码 tarball 独立构建(最后手段;GNU 官方在 Linux 仅分发源码)。
install_gcc_layer_source_build() {
    build_gcc_from_source
}

# 分层获取入口:依次尝试四层,任一层成功即返回 0。
install_gcc_layered() {
    if install_gcc_layer_upstream_binary; then
        log "acquired GCC via layer 1 (upstream official binary)"
        return 0
    fi
    log "layer 1 (upstream official binary) unavailable; falling back to layer 2"
    if install_gcc_layer_repo_package; then
        log "acquired GCC via layer 2 (third-party repo package)"
        return 0
    fi
    log "layer 2 (third-party repo package) unavailable; falling back to layer 2b"
    if install_gcc_layer_archive_binary; then
        log "acquired GCC via layer 2b (archived distro binary package)"
        return 0
    fi
    log "layer 2b (archived distro binary package) unavailable; falling back to layer 3"
    if install_gcc_layer_source_build; then
        log "acquired GCC via layer 3 (source build)"
        return 0
    fi
    log "ERROR: no GCC ${SOFTWARE_VERSION} acquisition layer succeeded"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -x "${GCC_BIN}" ]] || {
            log "ERROR: gcc is not present in the software image"
            return 40
        }
    else
        # 当场构建:装依赖 + 分层获取(官方二进制→三方仓库包→官方源码构建到私有前缀)。
        require_build_commands || return $?
        [[ ! -e "${BUILD_DIR}" && ! -e "${INSTALL_DIR}" ]] || {
            log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
            return 20
        }
        install_gcc_layered || return $?
        # layer2 可能改选发行版包的 gcc,重新解析工具路径后再统一校验版本。
        configure_runtime_paths || return $?
    fi

    actual="$(gcc_version_of "${GCC_BIN}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: gcc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "gcc ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare gcc for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    gcc version (default: ${SOFTWARE_VERSION})
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