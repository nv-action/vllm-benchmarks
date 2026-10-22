#!/usr/bin/env bash
# MySQL performance case (official prebuilt binary deployment).
#
# The software under test is fetched from the official MySQL 8.0 "Linux -
# Generic" prebuilt tarball and unpacked into a private directory.  The
# upstream Sysbench client is built in that directory during the test stage.
#
# The four framework stages map to: fetch+verify the tarball (build), launch a
# throwaway instance from it (start), run the single-instance Sysbench suite
# (test), and tear that instance down (stop).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_NAME="mysql"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-8.0.43}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

# Connection settings for the throwaway server and for the benchmark client.
MYSQL_HOST="${MYSQL_HOST:-127.0.0.1}"
MYSQL_PORT="${MYSQL_PORT:-}"
MYSQL_DB_USER="${MYSQL_DB_USER:-root}"
MYSQL_PASSWORD="${MYSQL_PASSWORD:-}"
MYSQL_DATA_ROOT="${MYSQL_DATA_ROOT:-}"
MYSQL_PREPARED_STMT_LIMIT=1048576

# Lifecycle paths (assigned in configure_runtime_paths).
MYSQL_BASE_DIR=""
MYSQLD_BIN=""
MYSQL_BIN=""
MYSQLADMIN_BIN=""
SERVICE_DIR=""
DATADIR=""
SOCKET_PATH=""
PID_FILE=""
ERR_LOG=""
MYSQL_TMP_DIR=""
MYSQL_SNAPSHOT_DIR=""
SYSBENCH_BIN=""
SYSBENCH_LUA_DIR=""

log() {
    printf '[mysql] %s\n' "$*"
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64)
            EXPECTED_ARCH="x86_64"
            ;;
        aarch64|arm64)
            EXPECTED_ARCH="aarch64"
            ;;
        *)
            EXPECTED_ARCH="${EXPECTED_ARCH,,}"
            ;;
    esac
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/mysql/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${MYSQL_DATA_ROOT}" ]]; then
        MYSQL_DATA_ROOT="${PERF_WORK_DIR}/data"
    fi
    MYSQL_BASE_DIR="${PERF_WORK_DIR}/mysql"
    MYSQLD_BIN="${MYSQL_BASE_DIR}/bin/mysqld"
    MYSQL_BIN="${MYSQL_BASE_DIR}/bin/mysql"
    MYSQLADMIN_BIN="${MYSQL_BASE_DIR}/bin/mysqladmin"
    SERVICE_DIR="${MYSQL_DATA_ROOT}"
    DATADIR="${SERVICE_DIR}/data"
    SOCKET_PATH="${SERVICE_DIR}/mysql.sock"
    PID_FILE="${SERVICE_DIR}/mysql.pid"
    ERR_LOG="${SERVICE_DIR}/mysql.err"
    MYSQL_TMP_DIR="${SERVICE_DIR}/tmp"
    MYSQL_SNAPSHOT_DIR="${PERF_WORK_DIR}/mysql-data-snapshot"
    SYSBENCH_BIN="${PERF_WORK_DIR}/sysbench-tools/sysbench-1.0.17/src/sysbench"
    SYSBENCH_LUA_DIR="${PERF_WORK_DIR}/sysbench-tools/sysbench-1.0.17/src/lua"
    TMPDIR="${MYSQL_TMP_DIR}"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    if [[ -z "${MYSQL_PORT}" ]]; then
        local checksum
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        MYSQL_PORT="$((20000 + ${checksum%% *} % 20000))"
    fi
    if [[ -z "${MYSQL_PASSWORD}" ]]; then
        MYSQL_PASSWORD="boostkit-perf-${PERF_RUN_ID}"
    fi
    if [[ ! "${MYSQL_PASSWORD}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: MYSQL_PASSWORD must use only letters, numbers, dots, underscores, or hyphens"
        return 10
    fi
    if [[ ! "${MYSQL_PORT}" =~ ^[0-9]+$ ]] || \
       (( MYSQL_PORT < 1024 || MYSQL_PORT > 65535 )); then
        log "ERROR: MYSQL_PORT must be an unprivileged TCP port: ${MYSQL_PORT}"
        return 10
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${SERVICE_DIR}" "${MYSQL_TMP_DIR}"
}

fetch_mysql_archive() {
    local archive_path
    local actual_md5
    local archive_md5
    local archive_name
    local download_url
    local local_archive_path
    local release_directory

    # Every supported artifact is declared with its official archive name and
    # checksum.  Adding a version means adding its two architecture entries
    # here; the download, extraction, and lifecycle logic remains unchanged.
    case "${SOFTWARE_VERSION}:${EXPECTED_ARCH}" in
        8.0.43:x86_64)
            release_directory="MySQL-8.0"
            archive_name="mysql-8.0.43-linux-glibc2.28-x86_64.tar.xz"
            archive_md5="9b6ef0d08e5506eab12fe2c1b619f664"
            ;;
        8.0.43:aarch64)
            release_directory="MySQL-8.0"
            archive_name="mysql-8.0.43-linux-glibc2.28-aarch64.tar.xz"
            archive_md5="c51923a9b540cd2f886734d3e0366e9a"
            ;;
        *)
            log "ERROR: no verified MySQL release is declared for ${SOFTWARE_VERSION} on ${EXPECTED_ARCH}"
            return 10
            ;;
    esac

    local_archive_path="/home/runner/software/mysql/${archive_name}"
    if [[ -f "${local_archive_path}" ]]; then
        archive_path="${local_archive_path}"
        log "using local MySQL archive ${archive_path}" >&2
    elif [[ -s "${PERF_WORK_DIR}/${archive_name}" ]]; then
        archive_path="${PERF_WORK_DIR}/${archive_name}"
        log "using cached MySQL archive ${archive_path}" >&2
    else
        archive_path="${PERF_WORK_DIR}/${archive_name}"
        download_url="https://dev.mysql.com/get/Downloads/${release_directory}/${archive_name}"
        log "downloading ${download_url}" >&2
        if command -v curl >/dev/null 2>&1; then
            if ! curl -fSL --retry 3 --connect-timeout 30 -o "${archive_path}" "${download_url}"; then
                rm -f "${archive_path}"
                log "ERROR: failed to download ${archive_name} from the official MySQL download endpoint" >&2
                return 30
            fi
        elif ! wget -q -O "${archive_path}" "${download_url}"; then
            rm -f "${archive_path}"
            log "ERROR: failed to download ${archive_name}" >&2
            return 30
        fi
    fi

    actual_md5="$(md5sum "${archive_path}")"
    actual_md5="${actual_md5%% *}"
    if [[ "${actual_md5}" != "${archive_md5}" ]]; then
        log "ERROR: MySQL archive checksum mismatch: expected ${archive_md5}, got ${actual_md5}" >&2
        return 30
    fi
    printf '%s\n' "${archive_path}"
}

extract_mysql_archive() {
    local archive="$1"
    local unpacked_directory

    rm -rf "${MYSQL_BASE_DIR}"
    log "extracting ${archive}"
    if [[ "${archive}" == *.tar.xz ]]; then
        tar -xJf "${archive}" -C "${PERF_WORK_DIR}"
    else
        tar -xzf "${archive}" -C "${PERF_WORK_DIR}"
    fi
    unpacked_directory="$(find "${PERF_WORK_DIR}" -maxdepth 1 -type d -name "mysql-${SOFTWARE_VERSION}-*" | head -n 1)"
    if [[ -z "${unpacked_directory}" ]]; then
        log "ERROR: cannot locate the unpacked mysql directory"
        return 40
    fi
    mv "${unpacked_directory}" "${MYSQL_BASE_DIR}"
}

require_mysql_tools() {
    local command_name package
    local packages=()

    # The MySQL binary archive needs the first group.  The remaining tools and
    # development headers are required later when this case builds the
    # pinned upstream Sysbench client from its original source.
    for command_name in md5sum tar ldd sed tee curl git make gcc autoreconf automake libtoolize pkg-config python3 sudo; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            md5sum|tee) package="coreutils" ;;
            tar) package="tar" ;;
            ldd) package="glibc-common" ;;
            sed) package="sed" ;;
            curl) package="curl" ;;
            git) package="git" ;;
            make) package="make" ;;
            gcc) package="gcc" ;;
            autoreconf) package="autoconf" ;;
            automake) package="automake" ;;
            libtoolize) package="libtool" ;;
            pkg-config) package="pkgconf-pkg-config" ;;
            python3) package="python3" ;;
            sudo) package="sudo" ;;
        esac
        log "missing required MySQL test command: ${command_name}"
        packages+=("${package}")
    done

    for package in libaio-devel openssl-devel; do
        if ! rpm -q "${package}" >/dev/null 2>&1; then
            log "missing required MySQL test package: ${package}"
            packages+=("${package}")
        fi
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install MySQL test prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing MySQL test packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install MySQL test prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install MySQL test prerequisites"
        return 30
    fi

    for command_name in md5sum tar ldd sed tee curl git make gcc autoreconf automake libtoolize pkg-config python3 sudo; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required MySQL test command remains unavailable: ${command_name}"
            return 30
        fi
    done
    for package in libaio-devel openssl-devel; do
        if ! rpm -q "${package}" >/dev/null 2>&1; then
            log "ERROR: required MySQL test package remains unavailable: ${package}"
            return 30
        fi
    done
}

configure_benchmark_account() {
    if [[ "${MYSQL_DB_USER}" == "root" ]]; then
        "${MYSQL_BIN}" --socket="${SOCKET_PATH}" -u root <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS sbtest;
FLUSH PRIVILEGES;
SQL
        return $?
    fi

    "${MYSQL_BIN}" --socket="${SOCKET_PATH}" -u root <<SQL
CREATE USER IF NOT EXISTS '${MYSQL_DB_USER}'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO '${MYSQL_DB_USER}'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS sbtest;
FLUSH PRIVILEGES;
SQL
}

build_mysql() {
    local archive_path
    local actual_version
    local runner_architecture

    initialize_runtime || return $?
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    require_mysql_tools || return $?
    if [[ -e "${MYSQL_BASE_DIR}/bin/mysqld" ]]; then
        log "ERROR: build output is not clean under ${PERF_WORK_DIR}"
        return 20
    fi

    archive_path="$(fetch_mysql_archive)" || return $?
    extract_mysql_archive "${archive_path}" || return $?

    if [[ ! -x "${MYSQLD_BIN}" || ! -x "${MYSQL_BIN}" ]]; then
        log "ERROR: mysqld/mysql binaries not found after extraction"
        return 40
    fi
    if ldd "${MYSQLD_BIN}" 2>&1 | grep -Eq 'libaio[^[:space:]]*[[:space:]]+=>[[:space:]]+not found'; then
        log "ERROR: libaio runtime is missing; provision it on the dedicated runner before this workflow"
        return 40
    fi
    actual_version="$("${MYSQLD_BIN}" --version 2>/dev/null | sed -nE 's/.*Ver ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p')"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: installed mysqld is ${actual_version}, expected ${SOFTWARE_VERSION}"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "mysqld ${actual_version} deployed from the official prebuilt tarball"
}

start_mysql_service() {
    local mysqld_user
    initialize_runtime || return $?
    if [[ ! -x "${MYSQLD_BIN}" || ! -x "${MYSQL_BIN}" ]]; then
        log "ERROR: MySQL is not deployed (run build first)"
        return 40
    fi

    if MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" -N -e "SELECT 1" \
        >/dev/null 2>&1; then
        log "ERROR: MySQL is already reachable on the task port ${MYSQL_HOST}:${MYSQL_PORT}"
        log "ERROR: refusing to benchmark a pre-existing service"
        return 20
    fi

    mysqld_user="$(id -un)"
    if [[ "$(id -u)" -eq 0 ]]; then
        mysqld_user="root"
    fi
    log "initializing a throwaway MySQL ${SOFTWARE_VERSION} instance under ${SERVICE_DIR}"

    rm -rf "${DATADIR}" "${MYSQL_TMP_DIR}"
    mkdir -p "${DATADIR}" "${MYSQL_TMP_DIR}"

    "${MYSQLD_BIN}" --no-defaults --initialize-insecure \
        --basedir="${MYSQL_BASE_DIR}" \
        --datadir="${DATADIR}" \
        --user="${mysqld_user}" \
        --log-error="${ERR_LOG}" || {
        log "ERROR: mysqld data directory initialization failed (see ${ERR_LOG})"
        return 40
    }

    launch_mysql_server prepare || return $?

    configure_benchmark_account || {
        log "ERROR: failed to configure benchmark account"
        return 40
    }

    if ! MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" -N -e "SELECT 1" \
        >/dev/null 2>&1; then
        log "ERROR: benchmark account is not reachable over TCP"
        return 40
    fi
    log "MySQL service is ready for data preparation"
}

launch_mysql_server() {
    local mode="$1"
    local attempt actual_limit
    local -a mode_options

    case "${mode}" in
        prepare)
            mode_options=(
                --innodb-flush-log-at-trx-commit=0
                --innodb-use-native-aio=OFF
                --skip-log-bin
            )
            ;;
        benchmark)
            mode_options=(
                --innodb-flush-log-at-trx-commit=1
                --innodb-use-native-aio=ON
                --log-bin="${DATADIR}/mysql-bin"
            )
            ;;
        *)
            log "ERROR: unsupported MySQL service mode: ${mode}"
            return 40
            ;;
    esac

    "${MYSQLD_BIN}" --no-defaults --daemonize \
        --basedir="${MYSQL_BASE_DIR}" \
        --datadir="${DATADIR}" \
        --tmpdir="${MYSQL_TMP_DIR}" \
        --user="$(id -un)" \
        --socket="${SOCKET_PATH}" \
        --pid-file="${PID_FILE}" \
        --port="${MYSQL_PORT}" \
        --bind-address="${MYSQL_HOST}" \
        --max-connections=2048 \
        --max-prepared-stmt-count="${MYSQL_PREPARED_STMT_LIMIT}" \
        --log-error="${ERR_LOG}" \
        "${mode_options[@]}" || {
        log "ERROR: failed to launch mysqld (see ${ERR_LOG})"
        return 40
    }

    for attempt in {1..60}; do
        if "${MYSQLADMIN_BIN}" --socket="${SOCKET_PATH}" -u root ping >/dev/null 2>&1; then
            if [[ "${mode}" == prepare ]]; then
                actual_limit="$("${MYSQL_BIN}" --socket="${SOCKET_PATH}" -u root -N \
                    -e 'SELECT @@GLOBAL.max_prepared_stmt_count')" || {
                    log "ERROR: cannot verify MySQL prepared-statement limit"
                    return 40
                }
            else
                actual_limit="$(MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
                    "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" -N \
                    -e 'SELECT @@GLOBAL.max_prepared_stmt_count')" || {
                    log "ERROR: cannot verify MySQL prepared-statement limit"
                    return 40
                }
            fi
            if [[ "${actual_limit}" != "${MYSQL_PREPARED_STMT_LIMIT}" ]]; then
                log "ERROR: MySQL max_prepared_stmt_count is ${actual_limit}, expected ${MYSQL_PREPARED_STMT_LIMIT}"
                return 40
            fi
            log "mysqld is ready on port ${MYSQL_PORT} (socket ${SOCKET_PATH})"
            return 0
        fi
        sleep 1
    done
    log "ERROR: mysqld did not become ready (see ${ERR_LOG})"
    return 40
}

shutdown_mysql_server() {
    local attempt

    if [[ ! -S "${SOCKET_PATH}" ]]; then
        return 0
    fi
    if ! MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQLADMIN_BIN}" \
        --socket="${SOCKET_PATH}" -u root shutdown; then
        log "ERROR: failed to shut down managed MySQL instance"
        return 50
    fi
    for attempt in {1..40}; do
        if [[ ! -S "${SOCKET_PATH}" ]]; then
            return 0
        fi
        sleep 0.5
    done
    log "ERROR: managed MySQL socket is still present after shutdown"
    return 50
}

run_sysbench() {
    local workload="$1" threads="$2" duration="$3" label="$4"
    local workload_script="${SYSBENCH_LUA_DIR}/oltp_${workload}.lua"
    local raw_output="${RESULTS_DIR}/sysbench_raw.log"
    local run_output="${RESULTS_DIR}/sysbench-runs/${label}.log"
    local action=run
    local -a command=(
        "${SYSBENCH_BIN}" --db-driver=mysql
        "--mysql-host=${MYSQL_HOST}" "--mysql-port=${MYSQL_PORT}"
        "--mysql-user=${MYSQL_DB_USER}" "--mysql-password=${MYSQL_PASSWORD}"
        --mysql-db=sbtest --table_size=10000000 --tables=64
        "--threads=${threads}" "--percentile=95" --report-interval=1
    )

    if [[ "${duration}" -gt 0 ]]; then
        command+=("--time=${duration}")
    fi
    if [[ "${label}" == prepare ]]; then
        action=prepare
    fi
    command+=("${workload_script}" "${action}")
    printf '# %s: oltp_%s %s, threads=%s, time=%s\n' \
        "${label}" "${workload}" "${action}" "${threads}" "${duration}" | tee -a "${raw_output}" "${run_output}"
    if ! LUA_PATH="${SYSBENCH_LUA_DIR}/?.lua;;" "${command[@]}" 2>&1 \
        | tee -a "${raw_output}" "${run_output}"; then
        log "ERROR: Sysbench ${label} failed"
        return 50
    fi
}

run_mysql_benchmarks() {
    local scenario threads snapshot_bytes available_bytes
    local -a threads_for_scenario
    local raw_output="${RESULTS_DIR}/sysbench_raw.log"
    local runs_dir="${RESULTS_DIR}/sysbench-runs"

    initialize_runtime || return $?
    require_mysql_tools || return $?
    if ! MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" -N -e 'SELECT 1' \
        >/dev/null 2>&1; then
        log "ERROR: MySQL is not reachable on ${MYSQL_HOST}:${MYSQL_PORT}"
        return 50
    fi
    if ! bash "${SCRIPT_DIR}/scripts/prepare_sysbench.sh" \
        "${PERF_WORK_DIR}/sysbench-tools" "${MYSQL_BASE_DIR}/bin/mysql_config"; then
        log "ERROR: failed to build pinned Sysbench client"
        return 50
    fi
    mkdir -p "${runs_dir}"
    : > "${raw_output}"

    MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" \
        -e 'CREATE DATABASE IF NOT EXISTS sbtest' || return 50
    log 'preparing 64 Sysbench tables with 10,000,000 rows each (256 threads)'
    run_sysbench read_write 256 0 prepare || return 50

    if [[ -e "${MYSQL_SNAPSHOT_DIR}" ]]; then
        log "ERROR: data snapshot already exists: ${MYSQL_SNAPSHOT_DIR}"
        return 50
    fi
    shutdown_mysql_server || return 50
    snapshot_bytes="$(du -sb "${DATADIR}" | awk '{print $1}')"
    available_bytes="$(df -B1 --output=avail "${PERF_WORK_DIR}" | tail -n 1 | tr -d ' ')"
    if (( available_bytes < snapshot_bytes + 10737418240 )); then
        log "ERROR: insufficient disk space for the private MySQL data snapshot"
        return 50
    fi
    log "copying prepared data to ${MYSQL_SNAPSHOT_DIR}"
    cp -a --reflink=never "${DATADIR}" "${MYSQL_SNAPSHOT_DIR}" || return 50
    launch_mysql_server benchmark || return 50
    MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" -N \
        -e 'SELECT @@innodb_flush_log_at_trx_commit, @@innodb_use_native_aio, @@log_bin, @@max_connections, @@max_prepared_stmt_count' \
        | { printf '# benchmark configuration: flush_at_commit native_aio log_bin max_connections max_prepared_stmt_count\n'; cat; } \
        | tee -a "${raw_output}" "${RESULTS_DIR}/mysql-benchmark-config.log" || return 50

    for scenario in read_only read_write write_only; do
        if [[ "${scenario}" == read_write ]]; then
            shutdown_mysql_server || return 50
            launch_mysql_server benchmark || return 50
        elif [[ "${scenario}" == write_only ]]; then
            shutdown_mysql_server || return 50
            rm -rf -- "${DATADIR}"
            cp -a --reflink=never "${MYSQL_SNAPSHOT_DIR}" "${DATADIR}" || return 50
            launch_mysql_server benchmark || return 50
        fi
        sleep 30
        log "warming up ${scenario}: 64 threads for 300 seconds"
        run_sysbench read_only 64 300 "warmup-${scenario}" || return 50
        threads_for_scenario=(1 32 128 256)
        for threads in "${threads_for_scenario[@]}"; do
            log "testing ${scenario}: ${threads} threads, 120 seconds"
            run_sysbench "${scenario}" "${threads}" 120 "run-${scenario}-threads${threads}" || return 50
        done
    done
    python3 "${SCRIPT_DIR}/scripts/collect_sysbench.py" \
        "${runs_dir}" "${RESULTS_DIR}/results.json" || return 50
    log 'single-instance Sysbench suite completed'
}

stop_mysql_service() {
    initialize_runtime || return $?
    shutdown_mysql_server || return 50
    if [[ "${SERVICE_DIR}" != "${PERF_WORK_DIR}/data" || "${SERVICE_DIR}" == / || -z "${SERVICE_DIR}" ]]; then
        log "ERROR: refusing to remove unexpected MySQL data directory: ${SERVICE_DIR}"
        return 50
    fi
    rm -rf -- "${SERVICE_DIR}"
    log "MySQL runtime data removed from ${SERVICE_DIR}"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping standalone work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log "external work directory was not removed: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/mysql/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/mysql" ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        rm -rf -- "${PERF_WORK_DIR}" || return 70
    fi
    log "cleaned standalone work directory: ${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_mysql_service
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_mysql_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed" finalize_status=0
    local command_status="passed"

    configure_runtime_paths || return $?
    STANDALONE_STOP_DONE=0
    STANDALONE_CLEANUP_DONE=0
    trap emergency_standalone_cleanup EXIT
    initialize_runtime || return $?

    if standalone_runtime system "${RESULTS_DIR}/system_info.json" && \
        standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
        :
    else
        stage_status=$?
        failed_stage="prepare"
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if build_mysql; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
                "${PERF_RUN_ID}"; then
                :
            else
                stage_status=$?
                failed_stage="build"
            fi
        else
            stage_status=$?
            failed_stage="build"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_mysql_service; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_mysql_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_mysql_service; then
        cleanup_status="failed"
    fi
    STANDALONE_STOP_DONE=1
    if ! standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"; then
        cleanup_status="failed"
    fi
    if ! cleanup_standalone_workdir; then
        cleanup_status="failed"
    fi
    STANDALONE_CLEANUP_DONE=1

    if [[ "${stage_status}" -ne 0 ]]; then
        command_status="failed"
    fi
    if standalone_runtime finalize \
        "${RESULTS_DIR}" \
        "${SOFTWARE_VERSION}" \
        "${EXPECTED_ARCH}" \
        "${PERF_RUN_ID}" \
        "${command_status}" \
        "${cleanup_status}" \
        "${failed_stage}"; then
        finalize_status=0
    else
        finalize_status=$?
    fi
    trap - EXIT
    if [[ "${stage_status}" -ne 0 ]]; then
        return "${stage_status}"
    fi
    if [[ "${cleanup_status}" != "passed" ]]; then
        return 70
    fi
    return "${finalize_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Deploy MySQL 8.0.x from the official prebuilt tarball and run Sysbench OLTP
benchmarks as a standalone performance evaluation.
Results default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       MySQL version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, MYSQL_DATA_ROOT
USAGE
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                if [[ "$#" -lt 2 ]]; then
                    log "ERROR: --version requires a value"
                    return 10
                fi
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                if [[ "$#" -lt 2 ]]; then
                    log "ERROR: --results-dir requires a value"
                    return 10
                fi
                RESULTS_DIR="$2"
                shift 2
                ;;
            --keep-workdir)
                STANDALONE_KEEP_WORK_DIR=1
                shift
                ;;
            -h|--help)
                usage
                return 0
                ;;
            *)
                log "ERROR: unsupported option: $1"
                usage
                return 10
                ;;
        esac
    done

    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    : > "${RESULTS_DIR}/results.log"
    local pipeline_status=0
    set +e
    run_mysql_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
