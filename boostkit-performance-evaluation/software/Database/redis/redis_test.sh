#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-7.0.15}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
REDIS_DATA_ROOT="${REDIS_DATA_ROOT:-}"
REDIS_SOURCE_URL="${REDIS_SOURCE_URL:-https://github.com/redis/redis.git}"
REDIS_SERVICE_PORT="${REDIS_SERVICE_PORT:-}"

SOURCE_DIR=""
REDIS_SERVER_BIN=""
REDIS_BENCHMARK_BIN=""
REDIS_CLI_BIN=""
SERVICE_DIR=""
PID_FILE=""
LOG_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[redis] %s\n' "$*"
}

initialize_runtime() {
    local checksum runner_architecture

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
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi

    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/redis/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${REDIS_DATA_ROOT}" ]]; then
        REDIS_DATA_ROOT="${PERF_WORK_DIR}/data"
    fi
    if [[ -z "${REDIS_SERVICE_PORT}" ]]; then
        checksum="$(printf '%s' "${PERF_RUN_ID}" | cksum)"
        REDIS_SERVICE_PORT="$((20000 + ${checksum%% *} % 20000))"
    fi
    if [[ ! "${REDIS_SERVICE_PORT}" =~ ^[0-9]+$ ]] || \
       (( REDIS_SERVICE_PORT < 1024 || REDIS_SERVICE_PORT > 65535 )); then
        log "ERROR: REDIS_SERVICE_PORT must be an unprivileged TCP port: ${REDIS_SERVICE_PORT}"
        return 10
    fi

    SOURCE_DIR="${PERF_WORK_DIR}/redis-source"
    REDIS_SERVER_BIN="${SOURCE_DIR}/src/redis-server"
    REDIS_BENCHMARK_BIN="${SOURCE_DIR}/src/redis-benchmark"
    REDIS_CLI_BIN="${SOURCE_DIR}/src/redis-cli"
    SERVICE_DIR="${REDIS_DATA_ROOT}"
    PID_FILE="${SERVICE_DIR}/redis.pid"
    LOG_FILE="${SERVICE_DIR}/redis.log"

    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE REDIS_SERVICE_PORT REDIS_DATA_ROOT
}

require_redis_commands() {
    local required package
    local packages=()

    for required in git python3 gcc make sed grep cksum nproc; do
        if command -v "${required}" >/dev/null 2>&1; then
            continue
        fi
        case "${required}" in
            git) package="git" ;;
            python3) package="python3" ;;
            gcc) package="gcc" ;;
            make) package="make" ;;
            sed) package="sed" ;;
            grep) package="grep" ;;
            cksum|nproc) package="coreutils" ;;
        esac
        log "missing required Redis command: ${required}"
        packages+=("${package}")
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install Redis build prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing Redis build packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install Redis build prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install Redis build prerequisites"
        return 30
    fi

    for required in git python3 gcc make sed grep cksum nproc; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required Redis command remains unavailable: ${required}"
            return 30
        fi
    done
}

build_redis() {
    local binary actual_version

    initialize_runtime || return $?
    require_redis_commands || return $?
    if [[ -e "${SOURCE_DIR}" ]]; then
        log "ERROR: source directory is not clean under ${PERF_WORK_DIR}"
        return 20
    fi

    log "cloning Redis ${SOFTWARE_VERSION} from ${REDIS_SOURCE_URL}"
    if ! git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${REDIS_SOURCE_URL}" "${SOURCE_DIR}"; then
        log "ERROR: failed to clone Redis ${SOFTWARE_VERSION}"
        return 30
    fi

    log "building Redis with the existing build command"
    if ! (
        cd "${SOURCE_DIR}"
        make -j"$(nproc)" BUILD_TLS=no
    ); then
        log "ERROR: Redis build failed"
        return 40
    fi
    for binary in "${REDIS_SERVER_BIN}" "${REDIS_BENCHMARK_BIN}" "${REDIS_CLI_BIN}"; do
        if [[ ! -x "${binary}" ]]; then
            log "ERROR: expected executable not found: ${binary}"
            return 40
        fi
    done

    actual_version="$("${REDIS_SERVER_BIN}" --version | sed -n 's/.*v=\([^ ]*\).*/\1/p' | head -n 1)"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: built Redis version ${actual_version:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
}

start_redis_service() {
    local attempt

    initialize_runtime || return $?
    if [[ ! -x "${REDIS_SERVER_BIN}" || ! -x "${REDIS_CLI_BIN}" ]]; then
        log "ERROR: Redis is not built; run the build stage first"
        return 40
    fi
    if [[ -e "${SERVICE_DIR}" ]]; then
        log "ERROR: Redis data directory already exists: ${SERVICE_DIR}"
        return 20
    fi
    if "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING 2>/dev/null | grep -qx PONG; then
        log "ERROR: port ${REDIS_SERVICE_PORT} already has a Redis service"
        return 20
    fi

    mkdir -p "${SERVICE_DIR}"
    log "starting isolated Redis on port ${REDIS_SERVICE_PORT} with data directory ${SERVICE_DIR}"
    if ! "${REDIS_SERVER_BIN}" \
        --bind 127.0.0.1 \
        --protected-mode yes \
        --port "${REDIS_SERVICE_PORT}" \
        --dir "${SERVICE_DIR}" \
        --daemonize yes \
        --pidfile "${PID_FILE}" \
        --logfile "${LOG_FILE}"; then
        log "ERROR: Redis service failed to start; see ${LOG_FILE}"
        return 40
    fi

    for attempt in {1..60}; do
        if "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING 2>/dev/null | grep -qx PONG; then
            log "Redis service is ready on port ${REDIS_SERVICE_PORT}"
            return 0
        fi
        sleep 1
    done
    log "ERROR: Redis service did not become ready; see ${LOG_FILE}"
    return 40
}

run_redis_benchmarks() {
    initialize_runtime || return $?
    if [[ ! -x "${REDIS_BENCHMARK_BIN}" || ! -x "${REDIS_CLI_BIN}" ]]; then
        log "ERROR: Redis benchmark tools are unavailable; run the build stage first"
        return 50
    fi
    if ! "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING | grep -qx PONG; then
        log "ERROR: Redis service is not running on port ${REDIS_SERVICE_PORT}"
        return 50
    fi

    if ! python3 "${SCRIPT_DIR}/scripts/benchmark_redis.py" \
        "${REDIS_BENCHMARK_BIN}" \
        "${RESULTS_DIR}/redis_benchmark_raw.log" \
        "${RESULTS_DIR}/benchmark_redis.json"; then
        log "ERROR: Redis SET/GET throughput benchmark failed"
        return 50
    fi
}

stop_redis_service() {
    local pid="" attempt

    initialize_runtime || return $?
    if [[ -f "${PID_FILE}" ]]; then
        pid="$(<"${PID_FILE}")"
    fi
    if [[ -x "${REDIS_CLI_BIN}" ]]; then
        "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" \
            SHUTDOWN NOSAVE >/dev/null 2>&1 || true
    fi
    if [[ "${pid}" =~ ^[0-9]+$ ]]; then
        for attempt in {1..20}; do
            kill -0 "${pid}" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "${pid}" 2>/dev/null; then
            kill -TERM "${pid}" 2>/dev/null || true
            sleep 1
        fi
        if kill -0 "${pid}" 2>/dev/null; then
            kill -KILL "${pid}" 2>/dev/null || true
        fi
    fi
    if [[ -x "${REDIS_CLI_BIN}" ]] && \
       "${REDIS_CLI_BIN}" -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING >/dev/null 2>&1; then
        log "ERROR: Redis service is still reachable on port ${REDIS_SERVICE_PORT}"
        return 50
    fi
    if [[ -d "${SERVICE_DIR}" ]]; then
        if [[ "${SERVICE_DIR}" != "${REDIS_DATA_ROOT}" ]]; then
            log "ERROR: refusing to remove unexpected Redis data directory: ${SERVICE_DIR}"
            return 70
        fi
        rm -rf -- "${SERVICE_DIR}"
    fi
    log "Redis service stopped and task data directory removed"
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
    if [[ "${PERF_RUN_ID}" != local-* ]] || \
       [[ "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/redis/${SOFTWARE_VERSION}/${PERF_RUN_ID}" ]]; then
        log "ERROR: refusing to remove unexpected standalone work directory: ${PERF_WORK_DIR}"
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
        stop_redis_service
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_redis_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed" finalize_status=0
    local command_status="passed"

    initialize_runtime || return $?
    STANDALONE_STOP_DONE=0
    STANDALONE_CLEANUP_DONE=0
    trap emergency_standalone_cleanup EXIT

    if standalone_runtime system "${RESULTS_DIR}/system_info.json" && \
       standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
        :
    else
        stage_status=$?
        failed_stage="prepare"
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if build_redis; then
            if ! standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
                "${PERF_RUN_ID}"; then
                stage_status=40
                failed_stage="build"
            fi
        else
            stage_status=$?
            failed_stage="build"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_redis_service; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_redis_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_redis_service; then
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

Build Redis, start an isolated service, run 40/60/80-client SET/GET
benchmark, collect the environment and report, then clean this run's data.

Options:
  --version VERSION       Redis version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated build work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, REDIS_DATA_ROOT,
  REDIS_SOURCE_URL, REDIS_SERVICE_PORT
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

    initialize_runtime || return $?
    : > "${RESULTS_DIR}/results.log"
    local pipeline_status=0
    set +e
    run_redis_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
