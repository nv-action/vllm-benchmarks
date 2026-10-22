#!/usr/bin/env bash
# MySQL 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
set -euo pipefail

SOFTWARE_NAME="mysql"
CATEGORY="Database"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-8.0.43}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# 一次性实例与本任务账号的连接参数(端口/密码由 run_id 派生,互不冲突)。
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-}"
MYSQL_DB_USER="${MYSQL_DB_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
MYSQL_DATA_ROOT="${MYSQL_DATA_ROOT:-}"
# 官方二进制包优先取用的离线目录,沿用性能用例的约定。
MYSQL_OFFLINE_DIR="${MYSQL_OFFLINE_DIR:-/home/runner/software/mysql}"
MYSQL_BASEDIR_OVERRIDE="${MYSQL_BASEDIR_OVERRIDE:-}"

# 生命周期路径(在 configure_runtime_paths 中赋值)。
MYSQL_BASE_DIR=""
MYSQLD_BIN=""
MYSQL_BIN=""
MYSQLADMIN_BIN=""
MYSQLSLAP_BIN=""
SERVICE_DIR=""
DATADIR=""
SOCKET_PATH=""
PID_FILE=""
ERR_LOG=""
MYSQL_TMP_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() { printf '[mysql-func] %s\n' "$*"; }

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

# 解析命令路径并解析软链,便于从 mysqld 反推 basedir。
resolve_command() {
    local name="$1" path candidate
    path="$(command -v "${name}" 2>/dev/null || true)"
    if [[ -n "${path}" ]]; then
        if command -v readlink >/dev/null 2>&1; then
            readlink -f "${path}" 2>/dev/null || printf '%s\n' "${path}"
        else
            printf '%s\n' "${path}"
        fi
        return 0
    fi
    for candidate in "/usr/sbin/${name}" "/usr/bin/${name}" "/usr/local/mysql/bin/${name}" "/usr/local/bin/${name}"; do
        if [[ -x "${candidate}" ]]; then
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
    if [[ -z "${MYSQL_PORT}" ]]; then
        local checksum
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        MYSQL_PORT="$((20000 + ${checksum%% *} % 20000))"
    fi
    if [[ -z "${MYSQL_PASSWORD}" ]]; then
        MYSQL_PASSWORD="boostkit-func-${PERF_RUN_ID}"
    fi
    [[ "${MYSQL_PASSWORD}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log "ERROR: unsafe MYSQL_PASSWORD"
        return 10
    }
    [[ "${MYSQL_PORT}" =~ ^[0-9]+$ ]] && ((MYSQL_PORT >= 1024 && MYSQL_PORT <= 65535)) || {
        log "ERROR: invalid MYSQL_PORT: ${MYSQL_PORT}"
        return 10
    }

    if [[ -z "${MYSQL_DATA_ROOT}" ]]; then
        MYSQL_DATA_ROOT="${PERF_WORK_DIR}/data"
    fi
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好软件:仅定位可执行文件,不构建。
        MYSQLD_BIN="$(resolve_command mysqld || true)"
        MYSQL_BIN="$(resolve_command mysql || true)"
        MYSQLADMIN_BIN="$(resolve_command mysqladmin || true)"
        MYSQLSLAP_BIN="$(resolve_command mysqlslap || true)"
        if [[ -n "${MYSQL_BASEDIR_OVERRIDE}" ]]; then
            MYSQL_BASE_DIR="${MYSQL_BASEDIR_OVERRIDE}"
        elif [[ -n "${MYSQLD_BIN}" ]]; then
            MYSQL_BASE_DIR="$(dirname "$(dirname "${MYSQLD_BIN}")")"
        fi
    else
        # 当场构建(此处为“解压官方二进制包”):全部落在任务私有目录。
        MYSQL_BASE_DIR="${PERF_WORK_DIR}/mysql"
        MYSQLD_BIN="${MYSQL_BASE_DIR}/bin/mysqld"
        MYSQL_BIN="${MYSQL_BASE_DIR}/bin/mysql"
        MYSQLADMIN_BIN="${MYSQL_BASE_DIR}/bin/mysqladmin"
        MYSQLSLAP_BIN="${MYSQL_BASE_DIR}/bin/mysqlslap"
    fi
    SERVICE_DIR="${MYSQL_DATA_ROOT}"
    DATADIR="${SERVICE_DIR}/data"
    SOCKET_PATH="${SERVICE_DIR}/mysql.sock"
    PID_FILE="${SERVICE_DIR}/mysql.pid"
    ERR_LOG="${SERVICE_DIR}/mysql.err"
    MYSQL_TMP_DIR="${SERVICE_DIR}/tmp"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export MYSQL_HOST MYSQL_PORT MYSQL_DB_USER MYSQL_PASSWORD MYSQL_DATA_ROOT
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${SERVICE_DIR}" "${MYSQL_TMP_DIR}"
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

# Ubuntu 24.04+ 的 libaio1t64 只装出 libaio.so.1t64*,而 MySQL 官方二进制硬依赖
# libaio.so.1,名字对不上时 ldd 永远是 "libaio.so.1 => not found"。缺 libaio.so.1 时
# 在库目录里补一个 compat 软链并刷新缓存(容器内操作,随容器一起销毁)。
ensure_libaio_soname_compat() {
    local lib_dir real candidate
    for lib_dir in "/usr/lib/$(uname -m)-linux-gnu" /usr/lib64 "/lib/$(uname -m)-linux-gnu" /usr/lib; do
        [[ -d "${lib_dir}" ]] || continue
        [[ -e "${lib_dir}/libaio.so.1" ]] && return 0
        for real in "${lib_dir}"/libaio.so.1t64*; do
            [[ -e "${real}" ]] || continue
            candidate="$(basename "${real}")"
            if [[ "$(id -u)" -eq 0 ]]; then
                ln -sf "${candidate}" "${lib_dir}/libaio.so.1" || continue
                ldconfig || true
            else
                sudo -n ln -sf "${candidate}" "${lib_dir}/libaio.so.1" || continue
                sudo -n ldconfig || true
            fi
            log "linked ${lib_dir}/libaio.so.1 -> ${candidate}"
            return 0
        done
    done
    return 1
}

# libaio 运行库在不同发行版包名不同,逐个候选重试。
install_debian_libaio() {
    local candidate
    for candidate in libaio1t64 libaio1 libaio-dev; do
        if apt_install "${candidate}" >/dev/null 2>&1; then
            log "installed ${candidate}"
            ensure_libaio_soname_compat || true
            return 0
        fi
    done
    log "WARN: could not install a libaio package from apt"
    return 1
}

install_os_packages() {
    local logical family
    local -a rhel=() debian=()
    local want_debian_libaio=0
    for logical in "$@"; do
        case "${logical}" in
            git) rhel+=(git); debian+=(git) ;;
            curl) rhel+=(curl); debian+=(curl) ;;
            python3) rhel+=(python3); debian+=(python3) ;;
            tar) rhel+=(tar); debian+=(tar) ;;
            # 官方二进制包是 .tar.xz,基础镜像不含 xz 会导致解压失败。
            xz) rhel+=(xz); debian+=(xz-utils) ;;
            coreutils) rhel+=(coreutils); debian+=(coreutils) ;;
            build-tools) rhel+=(gcc gcc-c++ make tar gzip); debian+=(gcc g++ make tar gzip) ;;
            # 源码编译(最后手段)额外依赖:CMake、OpenSSL、ncurses、bison、libcurl(MySQL 强制 WITH_CURL=system)。
            cmake) rhel+=(cmake); debian+=(cmake) ;;
            mysql-source-deps)
                rhel+=(openssl-devel ncurses-devel bison bzip2-devel libcurl-devel pkgconf-pkg-config)
                debian+=(libssl-dev libncurses-dev bison libbz2-dev libcurl4-openssl-dev pkg-config)
                ;;
            libaio) rhel+=(libaio); want_debian_libaio=1 ;;
            # 通用二进制里的 mysqld 还链了 libnuma。
            numa) rhel+=(numactl-libs); debian+=(libnuma1) ;;
            # mysql/mysqlslap 这些客户端链了 ncurses(交互编辑),缺了客户端连加载都过不去。
            ncurses) rhel+=(ncurses-libs); debian+=(libncurses6 libtinfo6) ;;
            *)
                log "ERROR: unknown logical package: ${logical}"
                return 30
                ;;
        esac
    done
    family="$(os_family)"
    case "${family}" in
        rhel)
            (( ${#rhel[@]} == 0 )) && return 0
            log "installing via dnf: ${rhel[*]}"
            dnf_install "${rhel[@]}"
            ;;
        debian)
            if (( ${#debian[@]} > 0 )); then
                log "installing via apt: ${debian[*]}"
                apt_install "${debian[@]}"
            fi
            (( want_debian_libaio )) && install_debian_libaio || true
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
}

# build 模式所需工具:官方二进制包解压 + 运行库。
require_build_commands() {
    local -a logical=()
    command -v curl >/dev/null 2>&1 || logical+=(curl)
    command -v tar >/dev/null 2>&1 || logical+=(tar)
    command -v xz >/dev/null 2>&1 || logical+=(xz)
    command -v md5sum >/dev/null 2>&1 || logical+=(coreutils)
    command -v python3 >/dev/null 2>&1 || logical+=(python3)
    if ! ldconfig -p 2>/dev/null | grep -q 'libaio\.so'; then
        logical+=(libaio)
    fi
    if ! ldconfig -p 2>/dev/null | grep -q 'libnuma\.so'; then
        logical+=(numa)
    fi
    if ! ldconfig -p 2>/dev/null | grep -q 'libncurses\.so\.6'; then
        logical+=(ncurses)
    fi
    if (( ${#logical[@]} > 0 )); then
        log "installing missing dependencies: ${logical[*]}"
        install_os_packages "${logical[@]}" || return 30
    fi
    # libnuma/ncurses 装好即用,libaio 则要保证官方二进制找得到 libaio.so.1。
    ensure_libaio_soname_compat || log "WARN: could not provide libaio.so.1 for the official binary"
    return 0
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