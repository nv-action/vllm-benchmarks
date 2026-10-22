#!/usr/bin/env bash
# openjdk (OpenJDK) 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="openjdk"
CATEGORY="Toolchain"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-28_19}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 上游官方二进制发布页(层1):jdk.java.net 按 feature 号列出 GA/EA 二进制,并附 .sha256。
#   页面 https://jdk.java.net/<feature>/ 里的下载链接指向 download.java.net。
JDK_PAGE_BASE="${JDK_PAGE_BASE:-https://jdk.java.net}"
JDK_DOWNLOAD_BASE="${JDK_DOWNLOAD_BASE:-https://download.java.net}"
# 三方仓库渠道(层2):Adoptium Temurin 的版本化下载 API(GA 之后才有对应版本)。
ADOPTIUM_API_BASE="${ADOPTIUM_API_BASE:-https://api.adoptium.net/v3/binary/latest}"

JDK_HOME=""
JAVA_BIN=""
JAVAC_BIN=""
JDK_ARCH=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[openjdk-func] %s\n' "$*"; }

# 解析 java -version 的版本号(java 版本打到 stderr),取首个引号内内容:28-ea / 21.0.9。
java_version_of() {
    "$1" -version 2>&1 | sed -n 's/^[^"]*"\([^"]*\)".*/\1/p' | head -n 1 || true
}

# 取 feature 号(版本串里首个数字段):28-ea -> 28,21.0.9 -> 21。
# feature 号是 GA 与 EA 之间唯一可比的标识,版本校验以此为准。
java_feature_of() {
    java_version_of "$1" | sed -n 's/^\([0-9][0-9]*\).*/\1/p' | head -n 1 || true
}

# 版本串可能是 "28"(纯 feature)或 "28_19"/"28+19"(feature_build,与镜像 tag 对齐);
# 上游 jdk.java.net 与 Adoptium 只按 feature 号取版本,故统一派生请求的 feature 号。
requested_feature() {
    printf '%s\n' "${SOFTWARE_VERSION%%[+_]*}"
}

# prebuilt 模式下定位镜像内已装好的 java:依次探测常见位置,
# 优先返回 feature 号与请求 feature 一致的二进制,否则返回首个可执行文件(交给版本校验报告差异)。
resolve_prebuilt_java() {
    local candidate fallback="" feature requested
    requested="$(requested_feature)"
    local -a candidates=(
        "$(command -v java 2>/dev/null || true)"
        "${JAVA_HOME:-}/bin/java"
        "/usr/lib/jvm/java-${requested}-openjdk/bin/java"
        "/usr/lib/jvm/java-${requested}-openjdk-${EXPECTED_ARCH}/bin/java"
        "/usr/lib/jvm/openjdk-${requested}/bin/java"
        "/usr/lib/jvm/default-java/bin/java"
        "/opt/java/openjdk/bin/java"
        "/opt/openjdk/bin/java"
        "/opt/openjdk-${requested}/bin/java"
        "/usr/local/openjdk/bin/java"
        "/usr/local/openjdk-${requested}/bin/java"
        # openeulertest/openjdk 镜像把 JDK 装在 /usr/local/java 并设了 JAVA_HOME。
        "/usr/local/java/bin/java"
        "/usr/bin/java"
        "/usr/local/bin/java"
    )
    for candidate in "${candidates[@]}"; do
        [[ -n "${candidate}" && -x "${candidate}" ]] || continue
        [[ -n "${fallback}" ]] || fallback="${candidate}"
        feature="$(java_feature_of "${candidate}")"
        if [[ "${feature}" == "${requested}" ]]; then
            printf '%s\n' "${candidate}"
            return 0
        fi
    done
    [[ -n "${fallback}" ]] && printf '%s\n' "${fallback}"
    return 0
}

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

configure_runtime_paths() {
    # 上游(jdk.java.net / Adoptium)的 linux 产物命名:x86 用 x64,aarch64 用 aarch64。
    case "$(normalize_arch "${EXPECTED_ARCH}")" in
        x86_64) JDK_ARCH="x64" ;;
        aarch64) JDK_ARCH="aarch64" ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
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
    JDK_HOME="${PERF_WORK_DIR}/jdk"
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位镜像内的 java,不构建。
        JAVA_BIN="$(resolve_prebuilt_java || true)"
        if [[ -n "${JAVA_BIN}" ]]; then
            JAVAC_BIN="${JAVA_BIN%/java}/javac"
        else
            JAVAC_BIN=""
        fi
    else
        # 当场安装:解压官方二进制到本次任务私有目录,不写系统路径。
        JAVA_BIN="${JDK_HOME}/bin/java"
        JAVAC_BIN="${JDK_HOME}/bin/javac"
    fi

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export JDK_PAGE_BASE JDK_DOWNLOAD_BASE ADOPTIUM_API_BASE
    export JDK_HOME JAVA_BIN JAVAC_BIN JDK_ARCH
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
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
            curl) rhel+=(curl); debian+=(curl) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            gzip) rhel+=(gzip); debian+=(gzip) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            ca-certificates) rhel+=(ca-certificates); debian+=(ca-certificates) ;;
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

require_build_commands() {
    local -a logical=()
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v gzip >/dev/null 2>&1 || logical+=(gzip)
    command -v sha256sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v grep >/dev/null 2>&1 || logical+=(coreutils)
    (( ${#logical[@]} == 0 )) && return 0
    log "installing missing dependencies: ${logical[*]}"
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
