#!/usr/bin/env bash
# Snappy 构建脚本:环境采集 + 准备被测软件(构建或定位)。
#   构建流程与参考镜像一致(openeuler-docker-images Others/snappy/1.1.7/24.03-lts-sp4):
#   GitHub 官方 tag 归档 + SHA-256 校验,cmake Release/共享库,make install DESTDIR,
#   安装前缀 /usr/local;不构建上游测试(参考流程即如此,镜像只交付库、头文件与 snappy.pc)。
#
#   FUNC_MODE=prebuilt - 镜像内已装好被测库,只定位交付物并核对版本,不重建;
#                        源码归档下载校验后保留到 check 阶段复核。
#   FUNC_MODE=build    - 照参考构建流程在任务私有工作区源码编译,DESTDIR 安装,不写入系统路径。
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

# 下载官方 tag 归档并对照 source-lock.json 校验,归档保留到 check 阶段复核。
# curl 参数与参考 Dockerfile 一致。
acquire_snappy_archive() {
    local archive="${SNAPPY_ARCHIVE}"
    log "downloading snappy ${SOFTWARE_VERSION} source archive"
    curl -fSL --connect-timeout 30 --max-time 600 --retry 3 --retry-delay 5 \
        -o "${archive}" \
        "${SNAPPY_RELEASE_BASE}/${SOFTWARE_VERSION}.tar.gz" || {
        log "ERROR: failed to download the snappy ${SOFTWARE_VERSION} source archive"
        return 30
    }
    # 归档先过 SHA-256 基线,通过后才允许解压;篡改的归档在这里被拒绝。
    python3 "${SCRIPT_DIR}/verify_source.py" snappy "${SOFTWARE_VERSION}" "${archive}" || {
        log "ERROR: snappy ${SOFTWARE_VERSION} source archive failed the SHA-256 baseline"
        return 30
    }
    sha256sum "${archive}" >>"${RESULTS_DIR}/source-sha256.txt" || return 30
}

# 解压归档(顶层目录名 snappy-${SOFTWARE_VERSION},与参考 Dockerfile 一致)。
extract_snappy_source() {
    [[ ! -e "${SNAPPY_SOURCE_DIR}" ]] || {
        log "ERROR: source directory is not clean: ${SNAPPY_SOURCE_DIR}"
        return 20
    }
    tar -zxf "${SNAPPY_ARCHIVE}" -C "${PERF_WORK_DIR}" || {
        log "ERROR: failed to extract the snappy ${SOFTWARE_VERSION} source archive"
        return 30
    }
    [[ -f "${SNAPPY_SOURCE_DIR}/CMakeLists.txt" ]] || {
        log "ERROR: extracted snappy source tree is incomplete: ${SNAPPY_SOURCE_DIR}"
        return 40
    }
}

# 照参考 Dockerfile 的构建流程:cmake Release/共享库 + make + make install DESTDIR。
build_snappy_from_source() {
    [[ ! -e "${SNAPPY_BUILD_DIR}" ]] || {
        log "ERROR: build directory is not clean: ${SNAPPY_BUILD_DIR}"
        return 20
    }
    log "configuring snappy (shared library, prefix=/usr/local)"
    # -DSNAPPY_BUILD_TESTS=OFF:参考镜像交付物不含测试;且显式关闭可避免装有
    #   系统 GTest 的环境触发 snappy 1.1.7 上游的 GTEST_LIBRARIES 链接缺陷。
    # -DCMAKE_POLICY_VERSION_MINIMUM=3.5:snappy 1.1.7 的 cmake_minimum_required(3.1)
    #   会被 CMake 4(Ubuntu 26.04)直接拒绝;此开关在旧 CMake 下仅是无害缓存变量。
    cmake -DCMAKE_BUILD_TYPE=Release \
        -DCMAKE_INSTALL_PREFIX=/usr/local \
        -DBUILD_SHARED_LIBS=ON \
        -DSNAPPY_BUILD_TESTS=OFF \
        -DCMAKE_POLICY_VERSION_MINIMUM=3.5 \
        -S "${SNAPPY_SOURCE_DIR}" -B "${SNAPPY_BUILD_DIR}" || {
        log "ERROR: cmake configure of snappy failed"
        return 40
    }
    log "building snappy"
    make -C "${SNAPPY_BUILD_DIR}" -j"$(nproc)" || {
        log "ERROR: snappy build failed"
        return 40
    }
    mkdir -p "${SNAPPY_DESTDIR}" || return 30
    make -C "${SNAPPY_BUILD_DIR}" install DESTDIR="${SNAPPY_DESTDIR}" || {
        log "ERROR: snappy install failed"
        return 40
    }
}

# prebuilt 模式:定位镜像交付物(库、CMake 版本文件),不重建被测库。
locate_image_snappy() {
    [[ -f "${SNAPPY_LIB_FILE}" ]] || {
        log "ERROR: libsnappy is missing in the software image: ${SNAPPY_LIB_FILE}"
        return 40
    }
    [[ -f "${SNAPPY_VERSION_CMAKE}" ]] || {
        log "ERROR: SnappyConfigVersion.cmake is missing in the software image: ${SNAPPY_VERSION_CMAKE}"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    # 版本白名单:仅 1.1.7(与参考 Dockerfile 及 source-lock.json 同批核对)。
    [[ "${SOFTWARE_VERSION}" == "1.1.7" ]] || {
        log "ERROR: this case only supports snappy 1.1.7 (reference Dockerfile), got ${SOFTWARE_VERSION}"
        return 10
    }
    # 先补齐依赖再下载归档:精简基础镜像(如 ubuntu:26.04)可能缺 curl/python3。
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        require_test_commands || return $?
    else
        require_build_commands || return $?
    fi
    acquire_snappy_archive || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        locate_image_snappy || return $?
        actual="$(read_snappy_version "${SNAPPY_VERSION_CMAKE}")"
    else
        extract_snappy_source || return $?
        build_snappy_from_source || return $?
        [[ -f "${SNAPPY_LIB_FILE}" && -f "${SNAPPY_VERSION_CMAKE}" ]] || {
            log "ERROR: snappy install did not produce the expected deliverables under ${SNAPPY_PREFIX}"
            return 40
        }
        actual="$(read_snappy_version "${SNAPPY_VERSION_CMAKE}")"
    fi
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: snappy ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "snappy ${actual} ready (mode=${FUNC_MODE}, prefix=${SNAPPY_PREFIX})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare snappy for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    snappy version (default: ${SOFTWARE_VERSION})
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
