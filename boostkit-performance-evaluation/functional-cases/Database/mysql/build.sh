#!/usr/bin/env bash
# MySQL 构建脚本:环境采集 + 准备被测软件(分层获取官方二进制或校验镜像)。
#   FUNC_MODE=prebuilt - 镜像已装好 MySQL,定位可执行文件并校验版本
#   FUNC_MODE=build    - 分层优先获取:上游官方二进制 → 三方/发行版仓库包 → 源码编译
#                        (build 模式运行在 ubuntu 官方基础镜像内)
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
#
# build 模式采用“分层优先获取”:按优先级依次尝试,每层取得产物后都必须校验实际版本
# == SOFTWARE_VERSION;不匹配或获取失败则清理该层残留并降级到下一层,三层都不可行才
# 让 install 失败。分层顺序:
#   1) upstream-binary 上游官方二进制:Oracle 官方 Linux Generic 预编译包
#      (config/distribution.yaml 记 primary=upstream-tarball、arm64_binary=yes;
#       note 说明官方提供 aarch64 TAR/RPM/DEB)。
#   2) repo-binary     三方/发行版仓库中“版本与请求版本一致”的二进制包(本用例取发行版
#      仓库的 mysql-server 包)。注:distribution.yaml 里 mysql 的 docker 渠道在 build 模式
#      (普通容器,无嵌套 docker/podman)内不可用,故不使用镜像分发;而 pypi/conda 渠道只提供
#      连接器(mysql-connector-python),不是 mysqld 服务端,故改用发行版仓库包。
#   3) source          上游源码 tarball 用 CMake 编译(最后手段,耗时最长)。

# 每个受支持的版本/架构都登记官方包名与 MD5;新增版本只需在此补两行。
mysql_archive_descriptor() {
    case "${SOFTWARE_VERSION}:$(normalize_arch "${EXPECTED_ARCH}")" in
        8.0.43:x86_64)
            MYSQL_RELEASE_DIR="MySQL-8.0"
            MYSQL_ARCHIVE_NAME="mysql-8.0.43-linux-glibc2.28-x86_64.tar.xz"
            MYSQL_ARCHIVE_MD5="9b6ef0d08e5506eab12fe2c1b619f664"
            ;;
        8.0.43:aarch64)
            MYSQL_RELEASE_DIR="MySQL-8.0"
            MYSQL_ARCHIVE_NAME="mysql-8.0.43-linux-glibc2.28-aarch64.tar.xz"
            MYSQL_ARCHIVE_MD5="c51923a9b540cd2f886734d3e0366e9a"
            ;;
        26.7.0:x86_64)
            MYSQL_RELEASE_DIR="MySQL-26.7"
            MYSQL_ARCHIVE_NAME="mysql-26.7.0-linux-glibc2.28-x86_64.tar.xz"
            MYSQL_ARCHIVE_MD5="f1b4f2a046493898d7a9d2d1c2bb43c8"
            ;;
        26.7.0:aarch64)
            MYSQL_RELEASE_DIR="MySQL-26.7"
            MYSQL_ARCHIVE_NAME="mysql-26.7.0-linux-glibc2.28-aarch64.tar.xz"
            MYSQL_ARCHIVE_MD5="7226ab9a5098190c130a2e97f4891561"
            ;;
        *)
            # 不作为硬错误:该层不可用即降级到下一层。
            log "no verified official MySQL release declared for ${SOFTWARE_VERSION} on ${EXPECTED_ARCH}"
            return 1
            ;;
    esac
}

# 离线包优先(沿用性能用例约定):/home/runner/software/mysql/ → 任务目录缓存 → 官方下载。
# 注意:本函数用 stdout 返回归档路径,所以它内部的诊断信息必须写 stderr,
# 否则会被调用方的 archive_path="$(fetch_mysql_archive)" 一并捕获,路径里混进日志文本。
fetch_mysql_archive() {
    local archive_path="${PERF_WORK_DIR}/${MYSQL_ARCHIVE_NAME}"
    local local_archive_path="${MYSQL_OFFLINE_DIR}/${MYSQL_ARCHIVE_NAME}"
    local download_url actual_md5
    if [[ -f "${local_archive_path}" ]]; then
        archive_path="${local_archive_path}"
        log "using offline MySQL archive ${archive_path}" >&2
    elif [[ -s "${archive_path}" ]]; then
        log "using cached MySQL archive ${archive_path}" >&2
    else
        download_url="https://dev.mysql.com/get/Downloads/${MYSQL_RELEASE_DIR}/${MYSQL_ARCHIVE_NAME}"
        log "downloading ${download_url}" >&2
        if command -v curl >/dev/null 2>&1; then
            curl -fSL --retry 3 --connect-timeout 30 -o "${archive_path}" "${download_url}" || {
                rm -f "${archive_path}"
                log "failed to download ${MYSQL_ARCHIVE_NAME}" >&2
                return 1
            }
        elif command -v wget >/dev/null 2>&1; then
            wget -q -O "${archive_path}" "${download_url}" || {
                rm -f "${archive_path}"
                log "failed to download ${MYSQL_ARCHIVE_NAME}" >&2
                return 1
            }
        else
            log "curl or wget is required to download the MySQL archive" >&2
            return 1
        fi
    fi
    actual_md5="$(md5sum "${archive_path}")"
    actual_md5="${actual_md5%% *}"
    [[ "${actual_md5}" == "${MYSQL_ARCHIVE_MD5}" ]] || {
        log "MySQL archive checksum mismatch: expected ${MYSQL_ARCHIVE_MD5}, got ${actual_md5}" >&2
        return 1
    }
    printf '%s\n' "${archive_path}"
}

extract_mysql_archive() {
    local archive="$1" unpacked
    rm -rf -- "${MYSQL_BASE_DIR}"
    log "extracting ${archive}"
    if [[ "${archive}" == *.tar.xz ]]; then
        tar -xJf "${archive}" -C "${PERF_WORK_DIR}"
    else
        tar -xzf "${archive}" -C "${PERF_WORK_DIR}"
    fi
    unpacked="$(find "${PERF_WORK_DIR}" -maxdepth 1 -type d -name "mysql-${SOFTWARE_VERSION}-*" | head -n 1)"
    [[ -n "${unpacked}" ]] || {
        log "cannot locate the unpacked mysql directory"
        return 1
    }
    mv "${unpacked}" "${MYSQL_BASE_DIR}"
}

# 从 mysqld --version 解析出三段式版本号。
mysqld_version() {
    local binary="$1"
    [[ -x "${binary}" ]] || return 1
    "${binary}" --version 2>/dev/null | sed -nE 's/.*Ver ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n 1
}

write_actual_version() {
    local actual="$1"
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}"
}

# 通用降级工具:清理某一层可能留下的构建/解包残留。
mysql_reset_build_tree() {
    rm -rf -- "${MYSQL_BASE_DIR}"
}

# ---------- 分层获取 ----------

# 检查官方二进制的运行库是否齐全:mysqld 缺库是起不来,而 mysql/mysqlslap 这类客户端
# 缺库(ncurses 之类)是连进程都加载不了,所以四个可执行文件都要过一遍 ldd,并把每个
# 缺失的库逐行打印出来,便于一次看清。
mysql_check_binary_runtime_libs() {
    local binary missing found=0
    for binary in "${MYSQLD_BIN}" "${MYSQL_BIN}" "${MYSQLADMIN_BIN}" "${MYSQLSLAP_BIN}"; do
        [[ -x "${binary}" ]] || continue
        missing="$(ldd "${binary}" 2>&1 | grep 'not found' || true)"
        [[ -n "${missing}" ]] || continue
        log "$(basename "${binary}") misses runtime libraries:"
        while IFS= read -r line; do
            log "  ${line}"
        done <<<"${missing}"
        found=1
    done
    (( found == 0 ))
}

# 层 1:上游官方二进制(Linux Generic 预编译包),版本必须严格等于请求版本。
mysql_layer_upstream_binary() {
    local archive_path actual
    mysql_archive_descriptor || return 1
    archive_path="$(fetch_mysql_archive)" || return 1
    extract_mysql_archive "${archive_path}" || { mysql_reset_build_tree; return 1; }
    [[ -x "${MYSQLD_BIN}" && -x "${MYSQL_BIN}" ]] || { mysql_reset_build_tree; return 1; }
    mysql_check_binary_runtime_libs || {
        log "official binary is not runnable: runtime libraries are missing"
        mysql_reset_build_tree
        return 1
    }
    actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "official binary is ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        mysql_reset_build_tree
        return 1
    }
    return 0
}

# 层 2:三方/发行版仓库中与请求版本一致的二进制包。发行版仓库版本不匹配时自动降级。
mysql_layer_repo_binary() {
    local family root deb actual name
    family="$(os_family)"
    root="${PERF_WORK_DIR}/mysql-repo-stage"
    mysql_reset_build_tree
    rm -rf -- "${root}"
    mkdir -p "${root}"

    case "${family}" in
        debian)
            command -v apt-get >/dev/null 2>&1 && command -v dpkg >/dev/null 2>&1 || {
                log "apt-get/dpkg unavailable for the repository binary layer"
                return 1
            }
            # 发行版仓库里必须有 MySQL 8.0 服务端包;取不到即降级。
            (cd "${root}" && apt-get download "mysql-server-core-8.0" >/dev/null 2>&1) || {
                log "distro repository has no mysql-server-core-8.0 package"
                return 1
            }
            deb="$(find "${root}" -maxdepth 1 -name '*.deb' | head -n 1)"
            [[ -n "${deb}" ]] || return 1
            dpkg -x "${deb}" "${root}/root" || {
                log "failed to unpack the distro mysql package"
                return 1
            }
            mkdir -p "${MYSQL_BASE_DIR}"
            cp -a "${root}/root/usr/." "${MYSQL_BASE_DIR}/" 2>/dev/null || true
            # 统一成任务使用的 bin/ 布局(test/cleanup 依赖 ${MYSQL_BASE_DIR}/bin)。
            mkdir -p "${MYSQL_BASE_DIR}/bin"
            for name in mysqld mysql mysqladmin mysqlslap; do
                [[ -x "${MYSQL_BASE_DIR}/sbin/${name}" ]] && ln -sf "${MYSQL_BASE_DIR}/sbin/${name}" "${MYSQL_BASE_DIR}/bin/${name}"
                [[ -x "${MYSQL_BASE_DIR}/usr/bin/${name}" ]] && ln -sf "${MYSQL_BASE_DIR}/usr/bin/${name}" "${MYSQL_BASE_DIR}/bin/${name}"
            done
            ;;
        *)
            # build 矩阵仅使用 debian 家族的 ubuntu 基础镜像;其它家族此层不可用。
            log "repository binary layer is only wired for debian-family build images"
            return 1
            ;;
    esac

    [[ -x "${MYSQLD_BIN}" && -x "${MYSQL_BIN}" ]] || { mysql_reset_build_tree; return 1; }
    actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "repository package is ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        mysql_reset_build_tree
        return 1
    }
    return 0
}

# 源码编译(CMake)所需的额外依赖。
require_mysql_source_commands() {
    local -a logical=()
    command -v cmake >/dev/null 2>&1 || logical+=(cmake)
    command -v git >/dev/null 2>&1 || logical+=(git)
    command -v cc >/dev/null 2>&1 || logical+=(build-tools)
    (( ${#logical[@]} == 0 )) || {
        log "installing source-build dependencies: ${logical[*]}"
        install_os_packages "${logical[@]}" || return 30
    }
    install_os_packages mysql-source-deps || return 30
}

# 层 3:从上游源码 tarball 用 CMake 构建并安装到任务私有目录(最后手段)。
mysql_layer_source() {
    local src tarball url actual build_dir release_dir
    local -a downloader
    src="${PERF_WORK_DIR}/mysql-source"
    mysql_reset_build_tree
    rm -rf -- "${src}"
    mkdir -p "${src}"
    require_mysql_source_commands || return 1

    # 源码下载目录随主次版本变化(如 8.0.43 → MySQL-8.0,26.7.0 → MySQL-26.7)。
    release_dir="MySQL-${SOFTWARE_VERSION%.*}"
    tarball="mysql-${SOFTWARE_VERSION}.tar.gz"
    url="https://dev.mysql.com/get/Downloads/${release_dir}/${tarball}"
    if command -v curl >/dev/null 2>&1; then
        downloader=(curl -fSL --retry 3 --connect-timeout 30 -o "${src}/${tarball}")
    elif command -v wget >/dev/null 2>&1; then
        downloader=(wget -q -O "${src}/${tarball}")
    else
        log "curl or wget is required for the source layer"
        return 1
    fi
    log "downloading MySQL source ${url}"
    "${downloader[@]}" "${url}" || { log "failed to download MySQL source"; return 1; }
    tar -xzf "${src}/${tarball}" -C "${src}" || { log "failed to unpack MySQL source"; return 1; }
    build_dir="${src}/mysql-${SOFTWARE_VERSION}"
    [[ -d "${build_dir}" ]] || { log "MySQL source tree not found after unpack"; return 1; }

    log "building MySQL ${SOFTWARE_VERSION} from source (configure)"
    (cd "${build_dir}" && cmake . \
        -DCMAKE_INSTALL_PREFIX="${MYSQL_BASE_DIR}" \
        -DINSTALL_LAYOUT=STANDALONE \
        -DDOWNLOAD_BOOST=1 -DWITH_BOOST="${src}/boost" \
        -DWITH_UNIT_TESTS=OFF -DFORCE_INSOURCE_BUILD=1) || {
        log "MySQL CMake configure step failed"
        return 1
    }
    log "building MySQL ${SOFTWARE_VERSION} from source (make)"
    (cd "${build_dir}" && make -j"$(nproc 2>/dev/null || printf '1')") || {
        log "MySQL source build failed"
        return 1
    }
    (cd "${build_dir}" && make install) || { log "MySQL source install failed"; return 1; }

    [[ -x "${MYSQLD_BIN}" && -x "${MYSQL_BIN}" ]] || { mysql_reset_build_tree; return 1; }
    actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "source build produced ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        mysql_reset_build_tree
        return 1
    }
    return 0
}

# 分层调度器:依次尝试各层,任一层取得且版本匹配即成功;全部失败才返回非零。
acquire_mysql_build() {
    local layer
    for layer in upstream-binary repo-binary source; do
        log "build: trying acquisition layer '${layer}' for MySQL ${SOFTWARE_VERSION}"
        if "mysql_layer_${layer//-/_}"; then
            log "build: MySQL ${SOFTWARE_VERSION} obtained from layer '${layer}'"
            return 0
        fi
        log "build: layer '${layer}' unavailable or version mismatch; falling back"
    done
    log "ERROR: every acquisition layer failed for MySQL ${SOFTWARE_VERSION}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual=""
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位可执行文件并校验版本。
        [[ -x "${MYSQLD_BIN}" ]] || {
            log "ERROR: mysqld is not available in the software image"
            return 40
        }
        [[ -x "${MYSQL_BIN}" ]] || {
            log "ERROR: mysql client is not available in the software image"
            return 40
        }
        actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    else
        # 分层优先获取(build 模式):官方二进制 → 发行版仓库包 → 源码编译。
        require_build_commands || return $?
        [[ ! -e "${MYSQL_BASE_DIR}/bin/mysqld" ]] || {
            log "ERROR: build output is not clean under ${PERF_WORK_DIR}"
            return 20
        }
        acquire_mysql_build || return $?
        actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    fi

    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: mysqld ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    write_actual_version "${actual}" || return 40
    log "mysql ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare MySQL for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    MySQL version (default: ${SOFTWARE_VERSION})
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