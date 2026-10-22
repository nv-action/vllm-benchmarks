#!/usr/bin/env bash
# Apache Spark 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="spark"
CATEGORY="Bigdata"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-3.5.9}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# Spark 是 JVM 应用,官方 pre-built tarball 与架构无关(仅区分 Hadoop 版本)。
SPARK_RELEASE_URL="${SPARK_RELEASE_URL:-https://archive.apache.org/dist/spark}"
# 层 1 下载镜像源(空格分隔,按序尝试,接入 PERF_PROXY);
# SPARK_RELEASE_URL 指向的 archive.apache.org 持有全部历史版本,始终作为最终兜底。
SPARK_MIRROR_URLS="${SPARK_MIRROR_URLS:-https://mirrors.huaweicloud.com/apache/spark https://downloads.apache.org/dist/spark}"
SPARK_OFFLINE_DIR="${SPARK_OFFLINE_DIR:-/home/runner/software/spark}"
# build 模式分层获取(build.sh)用到的三方/源码渠道:
#   层2 = 三方仓库打包产物(PyPI pyspark,内含完整 Spark 运行时);层3 = 上游源码(最后手段)。
SPARK_PYPI_PACKAGE="${SPARK_PYPI_PACKAGE:-pyspark}"
SPARK_SOURCE_URL="${SPARK_SOURCE_URL:-https://github.com/apache/spark.git}"
# 预构建镜像若把 Spark 装在非标准位置,可用 SPARK_HOME_OVERRIDE 显式指定。
SPARK_HOME_OVERRIDE="${SPARK_HOME_OVERRIDE:-}"

SPARK_HOME=""
SPARK_LOCAL_MASTER="${SPARK_LOCAL_MASTER:-local[2]}"
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[spark-func] %s\n' "$*"; }

normalize_arch() {
    case "${1,,}" in
        x86_64 | amd64) printf 'x86_64\n' ;;
        aarch64 | arm64) printf 'aarch64\n' ;;
        *) printf '%s\n' "${1,,}" ;;
    esac
}

os_id() {
    sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' | tr 'A-Z' 'a-z'
}

os_family() {
    case "$(os_id)" in
        ubuntu | debian) printf 'debian\n' ;;
        openeuler | centos | rhel | fedora | kylin) printf 'rhel\n' ;;
        *) printf '%s\n' "$(os_id)" ;;
    esac
}

detect_os_label() {
    local id version_id
    id="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"')"
    version_id="$(sed -n 's/^VERSION_ID=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"')"
    if [[ -n "${id}" && -n "${version_id}" ]]; then
        printf '%s-%s\n' "${id,,}" "${version_id}"
    else
        printf 'unknown\n'
    fi
}

check_architecture() {
    local actual expected
    actual="$(normalize_arch "$(uname -m)")"
    expected="$(normalize_arch "${EXPECTED_ARCH}")"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: expected architecture ${expected}, runner is ${actual}"
        return 20
    }
}

# 定位 Spark 安装根目录(必须含 bin/spark-submit)。prebuilt 模式优先使用镜像内已有安装。
resolve_spark_home() {
    local candidate bin
    local -a candidates=(
        "${SPARK_HOME_OVERRIDE:-}"
        "/opt/spark"
        "/opt/spark-${SOFTWARE_VERSION}"
        "/usr/local/spark"
        "/usr/lib/spark"
        "${HOME:-/root}/spark"
    )
    bin="$(command -v spark-submit 2>/dev/null || true)"
    if [[ -n "${bin}" ]]; then
        bin="$(cd "$(dirname "${bin}")/.." 2>/dev/null && pwd || true)"
        [[ -z "${bin}" ]] || candidates=("${bin}" "${candidates[@]}")
    fi
    for candidate in "${candidates[@]}"; do
        if [[ -n "${candidate}" && -x "${candidate}/bin/spark-submit" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    return 1
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log "ERROR: unsafe PERF_RUN_ID: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-func/${SOFTWARE_NAME}/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${FUNC_OS}" ]]; then
        FUNC_OS="$(detect_os_label)"
    fi

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内的 Spark 安装(找不到时留空,由 install 阶段报错)。
        SPARK_HOME="$(resolve_spark_home || true)"
    else
        SPARK_HOME="${PERF_WORK_DIR}/spark"
    fi
    # local 模式使用独立临时目录,避免污染镜像内 SPARK_HOME 并在 cleanup 时统一清理。
    SPARK_LOCAL_DIRS="${PERF_WORK_DIR}/spark-local"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export SPARK_HOME SPARK_LOCAL_MASTER SPARK_LOCAL_DIRS
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${SPARK_LOCAL_DIRS}"
}

# ---------- 下载与 Java 环境 ----------

# 统一下载入口:curl 接入 PERF_PROXY(容器内除 dnf/apt 外的下载同样需要走代理)。
download_file() {
    local url="$1" dest="$2"
    local -a opts=(--fail --silent --show-error --location --retry 3 --connect-timeout 30 -o "${dest}")
    [[ -z "${PERF_PROXY:-}" ]] || opts+=(-x "${PERF_PROXY}")
    curl "${opts[@]}" "${url}"
}

# 读取默认 java 的主版本号;java 缺失或输出异常时返回空(不让 set -e 退出)。
java_major_version() {
    # java -version 的版本信息走 stderr,必须 2>&1 合并后才可解析。
    java -version 2>&1 | sed -nE '1s/.*version "([0-9]+).*/\1/p' || true
}

# prebuilt = 镜像验收:镜像内 Java 与被测 Spark 版本不兼容时按镜像问题上报,不做兜底安装。
check_image_java_compat() {
    local major
    major="$(java_major_version)"
    case "${SOFTWARE_VERSION}" in
        4.*)
            # Spark 4.x 需要 Java 17/21;${major:-0} 兼容 java 缺失/输出异常的情况。
            if (( ${major:-0} < 17 )); then
                log "ERROR: image problem: Apache Spark ${SOFTWARE_VERSION} requires Java 17+, but the image provides Java ${major:-unknown}"
                return 40
            fi
            ;;
    esac
}

# ---------- OS 感知的依赖安装(build 模式用) ----------

dnf_install() {
    local -a opts=()
    [[ -z "${PERF_PROXY:-}" ]] || opts+=("--setopt=proxy=${PERF_PROXY}")
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${opts[@]}" install -y "$@"
    else
        sudo -n dnf "${opts[@]}" install -y "$@"
    fi
}

apt_install() {
    local -a opts=()
    if [[ -n "${PERF_PROXY:-}" ]]; then
        opts+=(-o "Acquire::http::Proxy=${PERF_PROXY}" -o "Acquire::https::Proxy=${PERF_PROXY}")
    fi
    if [[ "$(id -u)" -eq 0 ]]; then
        DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" update &&
            DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y "$@"
    else
        sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" update &&
            sudo -n env DEBIAN_FRONTEND=noninteractive apt-get "${opts[@]}" install -y "$@"
    fi
}

install_os_packages() {
    local logical family
    local -a rhel=() debian=()
    for logical in "$@"; do
        case "${logical}" in
            python3) rhel+=(python3 python3-pip); debian+=(python3 python3-pip) ;;
            download-tools) rhel+=(curl tar gzip); debian+=(curl tar gzip) ;;
            jdk) rhel+=(java-17-openjdk-devel); debian+=(openjdk-17-jdk-headless) ;;
            git) rhel+=(git); debian+=(git) ;;
            maven) rhel+=(maven); debian+=(maven) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    (( ${#rhel[@]} == 0 )) && return 0
    case "${family}" in
        rhel)
            log "installing via dnf: ${rhel[*]}"
            dnf_install "${rhel[@]}"
            ;;
        debian)
            log "installing via apt: ${debian[*]}"
            apt_install "${debian[@]}"
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# build 模式所需工具:下载 + 解压 + JDK + python3(跑 pyspark smoke)。
require_build_commands() {
    local -a logical=()
    if ! command -v curl >/dev/null 2>&1 && ! command -v wget >/dev/null 2>&1; then
        logical+=(download-tools)
    fi
    command -v tar >/dev/null 2>&1 || logical+=(download-tools)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v java >/dev/null 2>&1 || logical+=(jdk)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

require_test_commands() {
    local -a logical=()
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    command -v java >/dev/null 2>&1 || logical+=(jdk)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing test dependencies: ${logical[*]}"
    install_os_packages "${logical[@]}" || return 30
}

json_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' | tr -d '\r\n'
}

# ---------- 独立运行共用的参数解析 ----------

parse_common_args() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                [[ "$#" -ge 2 ]] || { log "ERROR: --version requires a value"; return 10; }
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                [[ "$#" -ge 2 ]] || { log "ERROR: --results-dir requires a value"; return 10; }
                RESULTS_DIR="$2"
                shift 2
                ;;
            --os)
                [[ "$#" -ge 2 ]] || { log "ERROR: --os requires a value"; return 10; }
                FUNC_OS="$2"
                shift 2
                ;;
            --hardware)
                [[ "$#" -ge 2 ]] || { log "ERROR: --hardware requires a value"; return 10; }
                FUNC_HARDWARE="$2"
                shift 2
                ;;
            --mode)
                [[ "$#" -ge 2 ]] || { log "ERROR: --mode requires a value"; return 10; }
                FUNC_MODE="$2"
                shift 2
                ;;
            --keep-workdir)
                STANDALONE_KEEP_WORK_DIR=1
                shift
                ;;
            -h | --help)
                return 64
                ;;
            *)
                log "ERROR: unsupported option: $1"
                return 10
                ;;
        esac
    done
}