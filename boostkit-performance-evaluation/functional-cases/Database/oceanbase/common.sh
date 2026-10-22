#!/usr/bin/env bash
# OceanBase 功能测试共享库:由 build.sh / test.sh source。
# 提供运行路径初始化、OS 探测与依赖安装、JSON 辅助。
# OceanBase 属“部署服务型”软件:官方 oceanbase-ce rpm 自带 obshell 与
# profile/oceanbase-service.sh,解包后执行 start 即可完成首次 bootstrap 并启动,
# 无需 OceanBase Deployer(obd)。两条链路同构:
#   prebuilt - 预构建镜像内已解包好 rpm,直接 start
#   build    - 现场下载官方 rpm、解包到 /home/admin/oceanbase、start
set -euo pipefail

SOFTWARE_NAME="oceanbase"
CATEGORY="Database"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-5.0.1.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
FUNC_OS="${FUNC_OS:-}"
FUNC_HARDWARE="${FUNC_HARDWARE:-}"
FUNC_MODE="${FUNC_MODE:-build}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

# OceanBase 连接配置(observer 默认 root 无密码)。
OB_HOST="${OB_HOST:-127.0.0.1}"
OB_PORT="${OB_PORT:-}"
OB_RPC_PORT="${OB_RPC_PORT:-}"
OB_USER="${OB_USER:-root}"
OB_PASSWORD="${OB_PASSWORD:-}"
OB_DB="${OB_DB:-sbtest}"

# 上游 sysbench 只读烟测规模(远小于性能用例)。
OB_SYSBENCH_TABLES="${OB_SYSBENCH_TABLES:-1}"
OB_SYSBENCH_TABLE_SIZE="${OB_SYSBENCH_TABLE_SIZE:-1000}"
OB_SYSBENCH_TIME="${OB_SYSBENCH_TIME:-10}"
OB_SYSBENCH_THREADS="${OB_SYSBENCH_THREADS:-4}"

# 部署相关路径(在 configure_runtime_paths 中赋值)。
OB_DATA_DIR=""
OB_REDO_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

# oceanbase-ce rpm 的固定安装布局:脚本内的 prefix 与配置文件路径都是硬编码的,
# 与 openEuler 预构建镜像完全一致。
OB_RPM_ROOT="/home/admin/oceanbase"
OB_SERVICE_SCRIPT="${OB_RPM_ROOT}/profile/oceanbase-service.sh"
OB_CNF_FILE="/etc/oceanbase.cnf"
# 与官方镜像的 el7 源保持一致,避免与 el8 目录里的包内容产生差异。
OB_RPM_MIRROR_BASE="https://mirrors.oceanbase.com/community/stable/el/7"
OB_SHELL_PORT="${OB_SHELL_PORT:-2886}"

# observer 资源参数。rpm 自带模板的默认值是 memory_limit=6G / log_disk_size=13G,
# 这里收敛到刚好可用的 4G,两个硬约束(已实测):
#   - memory_limit 低于 4G 会被 observer 判为 OB_INVALID_CONFIG 直接退出;
#   - log_disk_size 不能低于 3 * memory_limit,即 4G 内存至少要 12G。
OB_MEMORY_LIMIT="${OB_MEMORY_LIMIT:-4G}"
OB_SYSTEM_MEMORY="${OB_SYSTEM_MEMORY:-1G}"
OB_DATAFILE_SIZE="${OB_DATAFILE_SIZE:-2G}"
OB_DATAFILE_MAXSIZE="${OB_DATAFILE_MAXSIZE:-20G}"
OB_LOG_DISK_SIZE="${OB_LOG_DISK_SIZE:-13G}"
# rpm 模板写死 cpu_count=16,小规格机器上会被 observer 判为非法配置,按实际核数给。
OB_CPU_COUNT="${OB_CPU_COUNT:-$(nproc 2>/dev/null || printf '16')}"

log() { printf '[oceanbase-func] %s\n' "$*"; }

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
    # 每个用例独占一个容器(独立 network namespace),端口不会互相冲突,因此两种模式
    # 都固定用 rpm 自带 oceanbase.cnf 的默认端口,与 openEuler 预构建镜像完全一致。
    OB_PORT="${OB_PORT:-2881}"
    [[ "${OB_PORT}" =~ ^[0-9]+$ ]] && ((OB_PORT >= 1024 && OB_PORT <= 65535)) || {
        log "ERROR: invalid OB_PORT: ${OB_PORT}"
        return 10
    }
    OB_RPC_PORT="${OB_RPC_PORT:-2882}"
    ((OB_RPC_PORT <= 65535)) || {
        log "ERROR: invalid OB_RPC_PORT: ${OB_RPC_PORT}"
        return 10
    }

    # observer 的数据目录放在任务私有目录,随容器删除与工作目录回收一起消失。
    OB_DATA_DIR="${PERF_WORK_DIR}/oceanbase-data"
    OB_REDO_DIR="${PERF_WORK_DIR}/oceanbase-redo"

    export SOFTWARE_NAME CATEGORY SOFTWARE_VERSION EXPECTED_ARCH FUNC_OS FUNC_HARDWARE FUNC_MODE
    export PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
    export OB_HOST OB_PORT OB_RPC_PORT OB_SHELL_PORT OB_USER OB_PASSWORD OB_DB
    export OB_DATA_DIR OB_REDO_DIR
    export OB_MEMORY_LIMIT OB_SYSTEM_MEMORY OB_DATAFILE_SIZE OB_DATAFILE_MAXSIZE OB_LOG_DISK_SIZE OB_CPU_COUNT
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

# ---------- observer 连通性工具 ----------

# 经 mysql 协议对 observer 执行 SQL(默认 root 无密码)。
ob_mysql() {
    local -a args=("mysql" "-h${OB_HOST}" "-P${OB_PORT}" "-u${OB_USER}")
    [[ -n "${OB_PASSWORD}" ]] && args+=("-p${OB_PASSWORD}")
    "${args[@]}" "$@"
}

check_observer_ready() {
    ob_mysql -N -e "SELECT 1" >/dev/null 2>&1
}

# 读取并规整 observer 的四段式版本号。
query_observer_version() {
    local value
    value="$(ob_mysql -N -s -e "SELECT version()" 2>/dev/null || true)"
    printf '%s' "${value}" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' | head -n 1 || true
}

# 版本比对:预构建镜像的 tag 用三段式(5.0.1),而 observer 自报四段式(5.0.1.0),
# 两者同属一条版本线,故按"段前缀"判定(请求版本是实际版本的前缀即视为一致)。
version_matches() {
    local actual="$1" expected="$2"
    [[ -n "${actual}" ]] || return 1
    [[ "${actual}" == "${expected}" || "${actual}" == "${expected}".* ]]
}

# 轮询等待 observer 就绪。
wait_observer_ready() {
    local attempts="${1:-120}" attempt
    for attempt in $(seq 1 "${attempts}"); do
        if check_observer_ready; then
            log "observer ready on ${OB_HOST}:${OB_PORT} (after ${attempt} checks)"
            return 0
        fi
        sleep 3
    done
    log "ERROR: observer did not become ready on ${OB_HOST}:${OB_PORT}"
    return 40
}

# 两种模式共用同一条启动路径:oceanbase-service.sh 会读取 /etc/oceanbase.cnf,
# 拉起 obshell,再由 obshell 完成首次 bootstrap 并启动 observer。
start_observer_service() {
    local origin="$1"
    if check_observer_ready; then
        log "an OceanBase observer is already reachable on ${OB_HOST}:${OB_PORT}; reusing it"
        return 0
    fi
    [[ -f "${OB_SERVICE_SCRIPT}" ]] || {
        log "ERROR: ${OB_SERVICE_SCRIPT} is missing; the oceanbase-ce rpm was not unpacked"
        return 30
    }
    log "starting the observer ${origin} (${OB_SERVICE_SCRIPT})"
    bash "${OB_SERVICE_SCRIPT}" start || {
        log "ERROR: failed to start the observer ${origin}"
        return 40
    }
    return 0
}

# prebuilt 模式:镜像内已解包好 oceanbase-ce rpm,直接启动镜像自带实例并复用。
start_image_observer() {
    start_observer_service "bundled in the prebuilt image"
}

# ---------- OS 感知的依赖安装 ----------

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

# ---------- 官方 oceanbase-ce rpm ----------
#
# 与 openEuler 预构建镜像完全同构:取官方 oceanbase-ce rpm,按包内布局解包到 /,
# 再用自带的 profile/oceanbase-service.sh 启动。obd 在本用例中不再出现。

# kunpeng920 没有 LSE atomics,必须用 .nonlse 变体;kunpeng950 用普通变体。
ob_rpm_suffix() {
    if grep -qw atomics /proc/cpuinfo 2>/dev/null; then
        printf '\n'
    else
        printf '.nonlse\n'
    fi
}

# 从官方 el7 源目录索引里挑出与请求版本同线、变体匹配的最高 oceanbase-ce 包,
# 以及与它同一 build 的 ce-libs 包(observer 的 libaio.so.1 由它提供)。
# 索引取不到或无匹配时输出为空并返回 0,由调用方判定为空即报错。
ob_rpm_plan() {
    local arch="$1" version="$2" suffix listing name base candidate="" best=""
    arch="$(normalize_arch "${arch}")"
    suffix="$(ob_rpm_suffix)"
    listing="$(curl -fsSL --retry 3 --connect-timeout 30 \
        "${OB_RPM_MIRROR_BASE}/${arch}/" 2>/dev/null || true)"
    [[ -n "${listing}" ]] || return 0
    while IFS= read -r name; do
        [[ -n "${name}" ]] || continue
        base="${name%.el7.${arch}.rpm}"
        case "${base}" in
            *.nonlse) [[ "${suffix}" == ".nonlse" ]] || continue ;;
            *) [[ -z "${suffix}" ]] || continue ;;
        esac
        candidate="${base#oceanbase-ce-}"
        candidate="${candidate%-*}"
        version_matches "${candidate}" "${version}" || continue
        if [[ -z "${best}" ]] ||
            [[ "$(printf '%s\n%s\n' "${best}" "${name}" | sort -V | tail -n 1)" == "${name}" ]]; then
            best="${name}"
        fi
    done <<<"$(printf '%s\n' "${listing}" |
        grep -oE "oceanbase-ce-[0-9][0-9A-Za-z._-]*\.el7\.${arch}\.rpm" |
        grep -v '^oceanbase-ce-libs-' | sort -u)"
    [[ -n "${best}" ]] || return 0
    printf '%s\n%s\n' "${best}" "oceanbase-ce-libs-${best#oceanbase-ce-}"
}

# 用 rpm 自带模板生成 /etc/oceanbase.cnf:端口、数据目录与资源参数按当前用例覆盖,
# 其余保持上游默认。obshell 会把这份 cnf 合并进 profile/oceanbase.json 后拉起 observer。
write_observer_cnf() {
    local template="${OB_RPM_ROOT}/profile/oceanbase.cnf" item key
    local -a overrides=(
        "ip=${OB_HOST}"
        "mysql_port=${OB_PORT}"
        "rpc_port=${OB_RPC_PORT}"
        "obshell_port=${OB_SHELL_PORT}"
        "data_dir=${OB_DATA_DIR}"
        "redo_dir=${OB_REDO_DIR}"
        "datafile_size=${OB_DATAFILE_SIZE}"
        "datafile_maxsize=${OB_DATAFILE_MAXSIZE}"
        "cpu_count=${OB_CPU_COUNT}"
        "memory_limit=${OB_MEMORY_LIMIT}"
        "system_memory=${OB_SYSTEM_MEMORY}"
        "log_disk_size=${OB_LOG_DISK_SIZE}"
    )
    [[ -f "${template}" ]] || {
        log "ERROR: ${template} is missing; the oceanbase-ce rpm was not unpacked"
        return 30
    }
    cp -f "${template}" "${OB_CNF_FILE}" || return 30
    for item in "${overrides[@]}"; do
        key="${item%%=*}"
        if grep -qE "^[[:space:]]*${key}=" "${OB_CNF_FILE}"; then
            sed -i -E "s|^[[:space:]]*${key}=.*|${item}|" "${OB_CNF_FILE}"
        else
            printf '%s\n' "${item}" >>"${OB_CNF_FILE}"
        fi
    done
    mkdir -p "${OB_DATA_DIR}" "${OB_REDO_DIR}" || return 30
}

install_oceanbase_rpm() {
    local arch rpm_dir url name ce_name libs_name
    local -a plan=()
    arch="$(normalize_arch "$(uname -m)")"
    rpm_dir="${PERF_WORK_DIR}/rpm"
    mkdir -p "${rpm_dir}" || return 30
    mapfile -t plan <<<"$(ob_rpm_plan "${arch}" "${SOFTWARE_VERSION}")"
    ce_name="${plan[0]:-}"
    libs_name="${plan[1]:-}"
    [[ -n "${ce_name}" && -n "${libs_name}" ]] || {
        log "ERROR: no oceanbase-ce ${SOFTWARE_VERSION} rpm for ${arch} on ${OB_RPM_MIRROR_BASE}"
        return 30
    }
    for name in "${ce_name}" "${libs_name}"; do
        url="${OB_RPM_MIRROR_BASE}/${arch}/${name}"
        log "downloading ${url}"
        curl -fsSL --retry 3 --connect-timeout 30 -o "${rpm_dir}/${name}" "${url}" || {
            log "ERROR: failed to download ${url}"
            return 30
        }
        # rpm 内是 home/admin/oceanbase/... 相对路径,解包到 / 即落到 rpm 约定的安装位置。
        rpm2cpio "${rpm_dir}/${name}" | (cd / && cpio -idm --quiet --no-absolute-filenames) || {
            log "ERROR: failed to unpack ${name}"
            return 30
        }
        rm -f "${rpm_dir}/${name}"
    done
    chmod +x "${OB_SERVICE_SCRIPT}" || return 30
    write_observer_cnf || return $?
    start_observer_service "unpacked from ${ce_name}"
}

install_oceanbase_dependencies() {
    local family
    local -a packages=() want=()
    family="$(os_family)"

    for want in python3 curl hostname; do
        command -v "${want}" >/dev/null 2>&1 || packages+=("${want}")
    done
    # mysql 只是命令名,各发行版的包名不同:Debian/Ubuntu 侧是 default-mysql-client,
    # rhel 家族才叫 mysql(直接 apt install mysql 会报 Unable to locate package)。
    if ! command -v mysql >/dev/null 2>&1; then
        case "${family}" in
            debian) packages+=("default-mysql-client") ;;
            *) packages+=("mysql") ;;
        esac
    fi
    command -v sysbench >/dev/null 2>&1 || packages+=("sysbench")
    # build 模式要现场解包官方 rpm;observer 依赖的 libaio.so.1 由 oceanbase-ce-libs
    # 自带(obshell 启动时会设 LD_LIBRARY_PATH=$prefix/lib),不需要系统 libaio。
    if [[ "${FUNC_MODE}" != "prebuilt" ]]; then
        packages+=(rpm cpio)
    fi

    case "${family}" in
        rhel)
            (( ${#packages[@]} == 0 )) && return 0
            log "installing via dnf: ${packages[*]}"
            dnf_install "${packages[@]}" || return 30
            ;;
        debian)
            (( ${#packages[@]} == 0 )) && return 0
            log "installing via apt: ${packages[*]}"
            apt_install "${packages[@]}" || return 30
            ;;
        *)
            log "ERROR: unsupported OS family: ${family}"
            return 30
            ;;
    esac
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