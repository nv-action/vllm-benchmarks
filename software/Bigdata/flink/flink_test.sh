#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2.3.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

FLINK_RELEASE_URL="${FLINK_RELEASE_URL:-https://downloads.apache.org/flink}"
FLINK_OFFLINE_DIR="${FLINK_OFFLINE_DIR:-/home/runner/software/flink}"
FLINK_RUNTIME_ROOT="${FLINK_RUNTIME_ROOT:-}"
FLINK_BENCHMARK_RECORDS=1000000
FLINK_BENCHMARK_PARALLELISM=1

FLINK_ARCHIVE_NAME=""
FLINK_ARCHIVE_SHA512=""
FLINK_HOME=""
FLINK_RUNTIME_DIR=""
FLINK_REST_PORT=""
FLINK_RPC_PORT=""
FLINK_BLOB_PORT=""
FLINK_TASKMANAGER_RPC_PORT=""
FLINK_REST_URL=""
FLINK_JOB_JAR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0

log() {
    printf '[flink] %s\n' "$*"
}

initialize_runtime() {
    local actual_arch character_code index port_offset=0

    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64) EXPECTED_ARCH="x86_64" ;;
        aarch64|arm64) EXPECTED_ARCH="aarch64" ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) actual_arch="x86_64" ;;
        aarch64|arm64) actual_arch="aarch64" ;;
        *) actual_arch="$(uname -m)" ;;
    esac
    if [[ "${actual_arch}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${actual_arch}"
        return 20
    fi
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/flink/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${FLINK_RUNTIME_ROOT}" ]]; then
        FLINK_RUNTIME_ROOT="${PERF_WORK_DIR}/runtime"
    fi

    case "${SOFTWARE_VERSION}" in
        2.3.0)
            FLINK_ARCHIVE_NAME="flink-2.3.0-bin-scala_2.12.tgz"
            FLINK_ARCHIVE_SHA512="e5863767caeaa7c72e45fc62d45f7df9f435a1c83aed813ea550db39e9221194d148ea4a6c3bfb5604335974729c579d48c6c4c3eb43502e37310a0bf982462a"
            ;;
        *)
            log "ERROR: no verified Apache Flink binary release is declared for ${SOFTWARE_VERSION}"
            return 10
            ;;
    esac

    FLINK_HOME="${PERF_WORK_DIR}/flink"
    FLINK_RUNTIME_DIR="${FLINK_RUNTIME_ROOT}"
    for ((index = 0; index < ${#PERF_RUN_ID}; index++)); do
        printf -v character_code '%d' "'${PERF_RUN_ID:index:1}"
        port_offset=$(((port_offset + character_code) % 10000))
    done
    FLINK_REST_PORT="$((20000 + port_offset))"
    FLINK_RPC_PORT="$((30000 + port_offset))"
    FLINK_BLOB_PORT="$((40000 + port_offset))"
    FLINK_TASKMANAGER_RPC_PORT="$((50000 + port_offset))"
    FLINK_REST_URL="http://127.0.0.1:${FLINK_REST_PORT}"
    FLINK_JOB_JAR="${PERF_WORK_DIR}/flink-pass-through-job.jar"
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${FLINK_RUNTIME_DIR}" || return 30
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE FLINK_HOME FLINK_RUNTIME_DIR FLINK_REST_PORT FLINK_REST_URL TMPDIR
}

run_as_root() {
    if [[ "$1" == dnf && -n "${PERF_PROXY:-}" ]]; then
        set -- "$1" "--setopt=proxy=${PERF_PROXY}" "${@:2}"
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
        return
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: root privileges are required to install missing dependencies"
        return 30
    fi
    sudo -n "$@"
}

install_flink_dependencies() {
    local command_name missing=0

    for command_name in curl sha512sum tar gzip install java javac jar python3; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            missing=1
            log "missing required Flink command: ${command_name}"
        fi
    done
    if [[ "${missing}" -ne 0 ]]; then
        log "installing missing Flink runtime dependencies"
        if ! command -v dnf >/dev/null 2>&1; then
            log "ERROR: dnf is required to install Flink dependencies"
            return 30
        fi
        run_as_root dnf install -y curl tar gzip coreutils python3 java-17-openjdk-devel || return 30
    fi
    for command_name in curl sha512sum tar gzip install java javac jar python3; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required Flink command remains unavailable: ${command_name}"
            return 30
        fi
    done
}

verify_java_runtime() {
    local major

    major="$(java -version 2>&1 | sed -nE '1{s/.*version "([0-9]+).*/\1/p;}')"
    case "${major}" in
        11|17|21) ;;
        *)
            log "ERROR: Flink ${SOFTWARE_VERSION} requires Java 11, 17, or 21; found: $(java -version 2>&1 | head -n 1)"
            return 30
            ;;
    esac
}

install_flink_distribution() {
    local archive="${PERF_WORK_DIR}/${FLINK_ARCHIVE_NAME}" actual_sha512 offline_archive

    offline_archive="${FLINK_OFFLINE_DIR}/${FLINK_ARCHIVE_NAME}"
    if [[ -f "${offline_archive}" ]]; then
        log "using local Apache Flink archive ${offline_archive}"
        install -m 0644 "${offline_archive}" "${archive}" || return 30
    else
        log "downloading Apache Flink ${SOFTWARE_VERSION} binary distribution"
        if ! curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
            "${FLINK_RELEASE_URL}/flink-${SOFTWARE_VERSION}/${FLINK_ARCHIVE_NAME}"; then
            log "ERROR: failed to download ${FLINK_ARCHIVE_NAME}"
            return 30
        fi
    fi
    actual_sha512="$(sha512sum "${archive}" | awk '{print $1}')"
    if [[ "${actual_sha512}" != "${FLINK_ARCHIVE_SHA512}" ]]; then
        log "ERROR: Apache Flink archive SHA-512 mismatch: ${FLINK_ARCHIVE_NAME}"
        return 30
    fi
    if [[ -e "${FLINK_HOME}" ]]; then
        log "ERROR: Flink installation directory is not clean: ${FLINK_HOME}"
        return 20
    fi
    mkdir -p "${FLINK_HOME}" || return 30
    if ! tar -xzf "${archive}" -C "${FLINK_HOME}" --strip-components=1; then
        log "ERROR: failed to extract ${FLINK_ARCHIVE_NAME}"
        return 30
    fi
    rm -f "${archive}"
}

build_pass_through_job() {
    local classes_dir="${PERF_WORK_DIR}/job-classes"

    mkdir -p "${classes_dir}" || return 40
    if ! javac -cp "${FLINK_HOME}/lib/*" -d "${classes_dir}" \
        "${SCRIPT_DIR}/src/main/java/org/boostkit/performance/flink/PassThroughJob.java"; then
        log "ERROR: failed to compile the standalone Flink pass-through job"
        return 40
    fi
    if ! jar --create --file "${FLINK_JOB_JAR}" \
        --main-class org.boostkit.performance.flink.PassThroughJob -C "${classes_dir}" .; then
        log "ERROR: failed to package the standalone Flink pass-through job"
        return 40
    fi
}

build_flink() {
    initialize_runtime || return $?
    install_flink_dependencies || return $?
    verify_java_runtime || return $?
    install_flink_distribution || return $?
    "${FLINK_HOME}/bin/flink" --version | grep -Fq "${SOFTWARE_VERSION}" || {
        log "ERROR: installed Apache Flink version does not contain ${SOFTWARE_VERSION}"
        return 40
    }
    build_pass_through_job || return $?
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "installed Apache Flink ${SOFTWARE_VERSION} and compiled the isolated benchmark job"
}

write_flink_configuration() {
    mkdir -p "${FLINK_RUNTIME_DIR}/logs" "${FLINK_RUNTIME_DIR}/pids" || return 40
    cat > "${FLINK_HOME}/conf/config.yaml" <<EOF
jobmanager.rpc.address: 127.0.0.1
jobmanager.bind-host: 127.0.0.1
jobmanager.rpc.port: ${FLINK_RPC_PORT}
taskmanager.bind-host: 127.0.0.1
taskmanager.rpc.port: ${FLINK_TASKMANAGER_RPC_PORT}
taskmanager.data.port: 0
rest.address: 127.0.0.1
rest.bind-address: 127.0.0.1
rest.port: ${FLINK_REST_PORT}
blob.server.port: ${FLINK_BLOB_PORT}
taskmanager.numberOfTaskSlots: ${FLINK_BENCHMARK_PARALLELISM}
parallelism.default: ${FLINK_BENCHMARK_PARALLELISM}
jobmanager.memory.process.size: 1024m
taskmanager.memory.process.size: 1024m
io.tmp.dirs: ${TMPDIR}
env.log.dir: ${FLINK_RUNTIME_DIR}/logs
env.pid.dir: ${FLINK_RUNTIME_DIR}/pids
EOF
}

wait_for_flink_rest() {
    local attempts=30

    while (( attempts > 0 )); do
        if python3 - "${FLINK_REST_URL}" <<'PY'
import sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/overview", timeout=3) as response:
    raise SystemExit(0 if response.status == 200 else 1)
PY
        then
            return 0
        fi
        sleep 1
        ((attempts--))
    done
    log "ERROR: Flink REST endpoint did not become ready: ${FLINK_REST_URL}"
    return 40
}

start_flink_runtime() {
    initialize_runtime || return $?
    if [[ ! -x "${FLINK_HOME}/bin/flink" || ! -f "${FLINK_JOB_JAR}" ]]; then
        log "ERROR: Apache Flink build artifacts are unavailable"
        return 40
    fi
    if [[ -e "${FLINK_RUNTIME_DIR}/started" ]]; then
        log "ERROR: Flink runtime is already marked as started"
        return 20
    fi
    write_flink_configuration || return $?
    if ! "${FLINK_HOME}/bin/jobmanager.sh" start; then
        log "ERROR: failed to start the Flink JobManager"
        return 40
    fi
    wait_for_flink_rest || return $?
    if ! "${FLINK_HOME}/bin/taskmanager.sh" start; then
        log "ERROR: failed to start the Flink TaskManager"
        return 40
    fi
    printf '%s\n' "${FLINK_REST_URL}" > "${FLINK_RUNTIME_DIR}/started"
    log "started isolated Flink standalone cluster at ${FLINK_REST_URL}"
}

copy_flink_logs() {
    local output_dir="${RESULTS_DIR}/flink-logs"

    rm -rf -- "${output_dir}"
    mkdir -p "${output_dir}" || return 50
    if ! find "${FLINK_RUNTIME_DIR}/logs" -maxdepth 1 -type f -print -quit | grep -q .; then
        log "ERROR: isolated Flink cluster did not produce any logs"
        return 50
    fi
    cp -a "${FLINK_RUNTIME_DIR}/logs/." "${output_dir}/" || return 50
}

run_flink_benchmark() {
    initialize_runtime || return $?
    if [[ ! -f "${FLINK_RUNTIME_DIR}/started" ]]; then
        log "ERROR: isolated Flink standalone cluster is not running"
        return 50
    fi
    if ! python3 "${SCRIPT_DIR}/scripts/run_flink_benchmark.py" \
        --flink "${FLINK_HOME}/bin/flink" \
        --job-jar "${FLINK_JOB_JAR}" \
        --rest-url "${FLINK_REST_URL}" \
        --records "${FLINK_BENCHMARK_RECORDS}" \
        --parallelism "${FLINK_BENCHMARK_PARALLELISM}" \
        --output "${RESULTS_DIR}/benchmark_flink.json"; then
        log "ERROR: standalone Flink pass-through benchmark failed"
        return 50
    fi
    copy_flink_logs || return $?
    log "completed standalone Flink pass-through benchmark"
}

stop_flink_runtime() {
    local stop_status=0

    initialize_runtime || return $?
    if [[ -x "${FLINK_HOME}/bin/taskmanager.sh" ]]; then
        "${FLINK_HOME}/bin/taskmanager.sh" stop-all || stop_status=50
    fi
    if [[ -x "${FLINK_HOME}/bin/jobmanager.sh" ]]; then
        "${FLINK_HOME}/bin/jobmanager.sh" stop || stop_status=50
    fi
    rm -f "${FLINK_RUNTIME_DIR}/started"
    if [[ "${FLINK_RUNTIME_DIR}" != "${FLINK_RUNTIME_ROOT}" ]]; then
        log "ERROR: refusing to clean an unexpected Flink runtime directory"
        return 70
    fi
    rm -rf -- "${FLINK_RUNTIME_DIR}"
    if [[ "${stop_status}" -ne 0 ]]; then
        log "ERROR: failed to stop every isolated Flink process"
        return "${stop_status}"
    fi
    log "stopped isolated Flink standalone cluster"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_work_dir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 || "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        return
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/flink/local-* || "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/flink" ]]; then
        log "ERROR: refusing to clean an unexpected standalone work directory"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}"
}

run_flink_standalone() {
    local stage_status=0 failed_stage='' cleanup_status='passed'

    initialize_runtime || return $?
    standalone_runtime system "${RESULTS_DIR}/system_info.json"
    standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"
    if build_flink; then
        :
    else
        stage_status=$?
        failed_stage='build'
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_flink_runtime; then
            :
        else
            stage_status=$?
            failed_stage='start'
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_flink_benchmark > "${RESULTS_DIR}/raw-output.log" 2>&1; then
            cat "${RESULTS_DIR}/raw-output.log"
        else
            stage_status=$?
            failed_stage='test'
            cat "${RESULTS_DIR}/raw-output.log"
        fi
    fi
    if ! stop_flink_runtime; then
        cleanup_status='failed'
        stage_status=70
    fi
    standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"
    standalone_runtime build-info "${RESULTS_DIR}/build_info.json" \
        "${SOFTWARE_VERSION}" "${PERF_ACTUAL_VERSION_FILE}" "${EXPECTED_ARCH}" "${PERF_RUN_ID}"
    standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
        "${PERF_RUN_ID}" "${stage_status}" "${cleanup_status}" "${failed_stage}"
    cleanup_standalone_work_dir
    return "${stage_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Install Apache Flink ${SOFTWARE_VERSION}, run one isolated standalone cluster,
and execute the finite stateless pass-through benchmark.

Options:
  --version VERSION       Flink version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated installation for debugging
  -h, --help              Show this help
USAGE
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version) SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir) RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) log "ERROR: unknown option: $1"; usage >&2; return 10 ;;
        esac
    done
    trap 'stop_flink_runtime >/dev/null 2>&1 || true' EXIT
    run_flink_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
