#!/usr/bin/env bash
# Apache Spark 构建脚本:环境采集 + 准备被测软件(分层获取或校验镜像内版本)。
#   FUNC_MODE=prebuilt - 校验镜像内 Spark 版本,不构建
#   FUNC_MODE=build    - 装 JDK/下载工具 + 按优先级分层获取:
#                        1) 上游官方 pre-built tarball  2) 三方仓库打包的同版本产物  3) 源码编译
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

spark_archive_name() {
    # 官方 pre-built 发行版命名:<version>-bin-hadoop3(Hadoop 3.x)。
    printf 'spark-%s-bin-hadoop3.tgz\n' "${SOFTWARE_VERSION}"
}

verify_java_runtime() {
    local major
    major="$(java_major_version)"
    case "${SOFTWARE_VERSION}" in
        4.*)
            # Spark 4.x 仅支持 Java 17/21。
            case "${major}" in
                17 | 21) ;;
                *)
                    log "ERROR: Apache Spark ${SOFTWARE_VERSION} requires Java 17/21; found: Java ${major:-unknown}"
                    return 30
                    ;;
            esac
            ;;
        *)
            # Spark 3.x 支持 Java 8/11/17。
            case "${major}" in
                8 | 11 | 17) ;;
                *)
                    log "ERROR: Apache Spark ${SOFTWARE_VERSION} supports Java 8/11/17; found: Java ${major:-unknown}"
                    return 30
                    ;;
            esac
            ;;
    esac
}

install_spark_distribution() {
    local archive_name archive offline_archive mirror downloaded
    archive_name="$(spark_archive_name)"
    archive="${PERF_WORK_DIR}/${archive_name}"
    offline_archive="${SPARK_OFFLINE_DIR}/${archive_name}"

    if [[ -f "${offline_archive}" ]]; then
        log "using local Apache Spark archive ${offline_archive}"
        install -m 0644 "${offline_archive}" "${archive}" || return 30
    else
        # 镜像源按序尝试(走 PERF_PROXY),archive.apache.org 持有全部历史版本作最终兜底。
        local -a mirrors=()
        read -ra mirrors <<<"${SPARK_MIRROR_URLS}" || true
        mirrors+=("${SPARK_RELEASE_URL}")
        downloaded=0
        for mirror in "${mirrors[@]}"; do
            log "downloading Apache Spark ${SOFTWARE_VERSION} from ${mirror}"
            if download_file "${mirror}/spark-${SOFTWARE_VERSION}/${archive_name}" "${archive}"; then
                downloaded=1
                break
            fi
            log "download from ${mirror} failed; trying the next mirror"
        done
        [[ "${downloaded}" -eq 1 ]] || {
            log "ERROR: failed to download ${archive_name} from any mirror"
            return 30
        }
    fi
    [[ ! -e "${SPARK_HOME}" ]] || {
        log "ERROR: Spark installation directory is not clean: ${SPARK_HOME}"
        return 20
    }
    mkdir -p "${SPARK_HOME}" || return 30
    if ! tar -xzf "${archive}" -C "${SPARK_HOME}" --strip-components=1; then
        log "ERROR: failed to extract ${archive_name}"
        return 30
    fi
    rm -f "${archive}"
}

# ---------- 分层获取(build 模式) ----------
# 优先级:1) 上游官方 pre-built tarball  2) 三方仓库打包的同版本产物  3) 源码编译。
# 每层获取后都用 bin/spark-submit --version 校验版本 == SOFTWARE_VERSION;
# 不匹配或获取失败即降级到下一层,三层都不可用才让 install 失败。

# 读取当前 SPARK_HOME 里 Spark 的实际版本(读不到时返回非零)。
spark_installed_version() {
    local home="${1:-${SPARK_HOME}}" actual raw
    [[ -x "${home}/bin/spark-submit" ]] || return 1
    # spark-submit 的版本信息(banner)走 stderr,必须 2>&1 合并后才可解析。
    raw="$("${home}/bin/spark-submit" --version 2>&1 || true)"
    actual="$(printf '%s\n' "${raw}" | sed -n 's/.*[Vv]ersion \([0-9][0-9.]*\).*/\1/p' | head -n 1 || true)"
    [[ -n "${actual}" ]] || return 1
    printf '%s\n' "${actual}"
}

spark_version_matches() {
    [[ "$(spark_installed_version || true)" == "${SOFTWARE_VERSION}" ]]
}

# 层 2:三方仓库打包的同版本二进制/包。
#   Spark 官方以二进制 tarball 与容器镜像(apache/spark)分发;build 模式运行在普通容器内,
#   没有嵌套 docker/podman,官方镜像无法直接使用,故本层改用与请求版本一致的 Python 打包
#   产物 pyspark(内含完整 Spark 运行时:jars 与 bin 启动脚本)作为等效三方包。
#   注意:PyPI pyspark 不含 Scala 示例 jar(examples/jars),check 阶段的上游示例可能不可用。
install_spark_from_package() {
    local target="${PERF_WORK_DIR}/spark-pypi" home
    command -v python3 >/dev/null 2>&1 || {
        log "layer 2: python3 is required to fetch the packaged Spark"
        return 30
    }
    rm -rf -- "${target}"
    mkdir -p "${target}" || return 30
    log "layer 2: fetching packaged Spark ${SOFTWARE_VERSION} (${SPARK_PYPI_PACKAGE})"
    if ! python3 -m pip install --disable-pip-version-check --no-deps \
        -i https://pypi.tuna.tsinghua.edu.cn/simple --target "${target}" \
        "${SPARK_PYPI_PACKAGE}==${SOFTWARE_VERSION}"; then
        log "layer 2: ${SPARK_PYPI_PACKAGE}==${SOFTWARE_VERSION} is not available from the package index"
        return 30
    fi
    home="${target}/pyspark"
    [[ -x "${home}/bin/spark-submit" ]] || {
        log "layer 2: packaged Spark ships no bin/spark-submit runtime"
        return 30
    }
    rm -rf -- "${SPARK_HOME}"
    mkdir -p "${SPARK_HOME}" || {
        log "layer 2: failed to create ${SPARK_HOME}"
        return 30
    }
    if ! cp -a "${home}/." "${SPARK_HOME}/"; then
        # 失败不再静默:带上磁盘水位,便于判断是不是空间/IO 问题。
        log "layer 2: failed to copy the packaged Spark into ${SPARK_HOME}; free space: $(df -h "${PERF_WORK_DIR}" 2>/dev/null | tail -n 1 || true)"
        return 30
    fi
    chmod +x "${SPARK_HOME}/bin/spark-submit" 2>/dev/null || true
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

# 层 3:源码编译(最后手段)。按 v<version> tag 克隆上游源码,用自带的 make-distribution 打包。
install_spark_from_source() {
    local src="${PERF_WORK_DIR}/spark-src" tgz
    ensure_source_build_tools || return $?
    rm -rf -- "${src}"
    log "layer 3: building Spark ${SOFTWARE_VERSION} from source (${SPARK_SOURCE_URL})"
    if ! GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${SPARK_SOURCE_URL}" "${src}"; then
        log "layer 3: no source tag v${SOFTWARE_VERSION} at ${SPARK_SOURCE_URL}"
        return 30
    fi
    if ! (cd "${src}" && ./dev/make-distribution.sh --tgz --name "spark-${SOFTWARE_VERSION}-local" \
        -DskipTests -Phadoop-3 -Phive); then
        log "layer 3: make-distribution of Spark ${SOFTWARE_VERSION} failed"
        return 30
    fi
    tgz="$(find "${src}" -maxdepth 1 -type f -name 'spark-*-bin-*.tgz' 2>/dev/null | sort | head -n 1 || true)"
    [[ -n "${tgz}" ]] || { log "layer 3: source build produced no binary distribution"; return 30; }
    rm -rf -- "${SPARK_HOME}"
    mkdir -p "${SPARK_HOME}" || return 30
    tar -xzf "${tgz}" -C "${SPARK_HOME}" --strip-components=1 || {
        log "layer 3: failed to extract the built Spark distribution"
        return 30
    }
}

# build 模式分层安装:任一层通过 bin/spark-submit --version 版本校验即停止。
install_spark_build() {
    if install_spark_distribution && spark_version_matches; then
        log "build layer 1/3 satisfied Apache Spark ${SOFTWARE_VERSION} (upstream tarball)"
        return 0
    fi
    log "build layer 1/3 did not provide Apache Spark ${SOFTWARE_VERSION}; degrading"
    rm -rf -- "${SPARK_HOME}"
    if install_spark_from_package && spark_version_matches; then
        log "build layer 2/3 satisfied Apache Spark ${SOFTWARE_VERSION} (packaged binary)"
        return 0
    fi
    log "build layer 2/3 did not provide Apache Spark ${SOFTWARE_VERSION}; degrading"
    rm -rf -- "${SPARK_HOME}"
    if install_spark_from_source && spark_version_matches; then
        log "build layer 3/3 satisfied Apache Spark ${SOFTWARE_VERSION} (source build)"
        return 0
    fi
    log "ERROR: no build layer provided Apache Spark ${SOFTWARE_VERSION}"
    rm -rf -- "${SPARK_HOME}"
    return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位并校验版本,不做任何构建。prebuilt 属于镜像验收,
        # 镜像内缺软件或 Java 版本不兼容都按镜像问题上报,不做兜底安装。
        SPARK_HOME="$(resolve_spark_home || true)"
        [[ -n "${SPARK_HOME}" && -x "${SPARK_HOME}/bin/spark-submit" ]] || {
            log "ERROR: image problem: Apache Spark is not present in the software image"
            return 40
        }
        require_test_commands || return $?
        check_image_java_compat || return $?
    else
        require_build_commands || return $?
        verify_java_runtime || return $?
        install_spark_build || return $?
    fi

    actual="$(spark_installed_version || true)"
    if [[ "${actual}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: Apache Spark ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        # prebuilt 模式把 spark-submit 的原始输出留给日志,便于定位镜像问题。
        if [[ -n "${SPARK_HOME}" && -x "${SPARK_HOME}/bin/spark-submit" ]]; then
            "${SPARK_HOME}/bin/spark-submit" --version 2>&1 | head -n 20 | sed 's/^/[spark-func]   /' || true
        fi
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "Apache Spark ${actual} ready at ${SPARK_HOME} (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare Apache Spark for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Spark version (default: ${SOFTWARE_VERSION})
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