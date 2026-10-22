#!/usr/bin/env bash
# OpenViking performance case (official PyPI wheel deployment).
#
# The software under test is the official openviking wheel for the requested
# release, installed into a per-run virtual environment inside this case's
# isolated work directory — the distribution package manager only ships one
# openviking build and cannot switch between releases.
#
# The benchmarks reuse the upstream openEuler reference scheme: the AGFS
# (RAGFS) binding client from openviking.pyagfs mounts an in-memory "memfs"
# filesystem and exercises write/read/stat/ls/rm/mkdir/grep operations across
# 1KB/64KB/1MB payloads, client initialization latency, and multithreaded
# write scaling.
#
# The four framework stages map to: create a venv and install the pinned
# official wheel (build), verify the deployed AGFS runtime with a smoke
# mount/read/write cycle (start), run the context and micro benchmark suites
# (test), and remove the venv (stop).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_NAME="openviking"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-0.4.17.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

# Fixed sources for the official wheel and its Python dependencies.
# Packages resolve through the Huawei PyPI mirror because domestic runners
# cannot reliably reach files.pythonhosted.org.
OPENVIKING_REPOSITORY="${OPENVIKING_REPOSITORY:-https://github.com/volcengine/OpenViking}"
OPENVIKING_PACKAGE_INDEX="${OPENVIKING_PACKAGE_INDEX:-https://mirrors.huaweicloud.com/repository/pypi/simple}"

# Benchmark iterations per operation (overridable).
OPENVIKING_ITERATIONS="${OPENVIKING_ITERATIONS:-3}"

# Lifecycle paths (assigned in configure_runtime_paths).
OPENVIKING_VENV=""
OPENVIKING_PYTHON=""

log() {
    printf '[openviking] %s\n' "$*"
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/openviking/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    # The framework pre-creates ${PERF_WORK_DIR}/venv before every stage, so
    # the task-private environment must live under a different name.
    OPENVIKING_VENV="${PERF_WORK_DIR}/openviking-venv"
    OPENVIKING_PYTHON="${OPENVIKING_VENV}/bin/python"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE OPENVIKING_ITERATIONS TMPDIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    if [[ ! "${OPENVIKING_ITERATIONS}" =~ ^[0-9]+$ ]] || \
       (( OPENVIKING_ITERATIONS < 1 )); then
        log "ERROR: OPENVIKING_ITERATIONS must be a positive integer: ${OPENVIKING_ITERATIONS}"
        return 10
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_openviking_tools() {
    local command_name package
    local packages=()

    # The benchmark runs on the system Python interpreter; the remaining tools
    # download the official wheel and record the console output.
    for command_name in python3 curl sha256sum tee sudo; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            python3) package="python3" ;;
            curl) package="curl" ;;
            sha256sum|tee) package="coreutils" ;;
            sudo) package="sudo" ;;
        esac
        log "missing required OpenViking test command: ${command_name}"
        packages+=("${package}")
    done
    if ! rpm -q python3-pip >/dev/null 2>&1; then
        log "missing required OpenViking test package: python3-pip"
        packages+=("python3-pip")
    fi

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install OpenViking test prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing OpenViking test packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install OpenViking test prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install OpenViking test prerequisites"
        return 30
    fi

    for command_name in python3 curl sha256sum tee sudo; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required OpenViking test command remains unavailable: ${command_name}"
            return 30
        fi
    done
    if ! rpm -q python3-pip >/dev/null 2>&1; then
        log "ERROR: required OpenViking test package remains unavailable: python3-pip"
        return 30
    fi
}

# The official openviking wheels only exist for the interpreter versions
# declared in the upstream release support matrix.  Adding a Python version
# or an openviking release means updating this declaration; wheel resolution
# fails loudly otherwise.
supported_python_version() {
    local python_version
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 30
    case "${python_version}" in
        3.10|3.11|3.12|3.13)
            printf '%s\n' "${python_version}"
            ;;
        *)
            log "ERROR: system python3 is ${python_version}, but the official OpenViking ${SOFTWARE_VERSION} wheels require one of 3.10-3.13"
            return 30
            ;;
    esac
}

build_openviking() {
    local wheel_version
    local python_version
    local runner_architecture

    initialize_runtime || return $?
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    require_openviking_tools || return $?
    if [[ -e "${OPENVIKING_VENV}" ]]; then
        log "ERROR: build output is not clean under ${OPENVIKING_VENV}"
        return 20
    fi

    python_version="$(supported_python_version)" || return $?
    log "system python3 is ${python_version}"
    log "creating the task-private virtual environment ${OPENVIKING_VENV}"
    if ! python3 -m venv "${OPENVIKING_VENV}"; then
        log "ERROR: failed to create the virtual environment"
        return 40
    fi
    if [[ ! -x "${OPENVIKING_PYTHON}" || ! -x "${OPENVIKING_VENV}/bin/pip" ]]; then
        log "ERROR: the virtual environment has no usable python/pip"
        return 40
    fi

    log "installing the official OpenViking ${SOFTWARE_VERSION} wheel from ${OPENVIKING_PACKAGE_INDEX}"
    if ! "${OPENVIKING_VENV}/bin/pip" install --no-cache-dir \
            --trusted-host mirrors.huaweicloud.com \
            --index-url "${OPENVIKING_PACKAGE_INDEX}" \
            "openviking==${SOFTWARE_VERSION}"; then
        log "ERROR: failed to install openviking==${SOFTWARE_VERSION} from the official package index"
        return 40
    fi

    wheel_version="$("${OPENVIKING_PYTHON}" -c 'import openviking; print(openviking.__version__)' 2>/dev/null)" || {
        log "ERROR: the installed openviking package cannot be imported"
        return 40
    }
    if [[ "${wheel_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: installed openviking is ${wheel_version}, expected ${SOFTWARE_VERSION}"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${wheel_version}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "openviking ${wheel_version} deployed from the official wheel"
}

start_openviking_runtime() {
    initialize_runtime || return $?
    if [[ ! -x "${OPENVIKING_PYTHON}" ]]; then
        log "ERROR: OpenViking is not deployed (run build first)"
        return 40
    fi

    if ! "${OPENVIKING_PYTHON}" - <<'OPENVIKING_SMOKE' >/dev/null 2>&1
from openviking.pyagfs import get_binding_client

BindingClient, _FileHandle = get_binding_client()
client = BindingClient()
mount_path = "viking://smoke/"
client.mount("memfs", mount_path)
try:
    payload = b"openviking-smoke-payload"
    client.write(f"{mount_path}smoke.bin", payload)
    data = client.read(f"{mount_path}smoke.bin")
    if not isinstance(data, (bytes, bytearray)) or bytes(data) != payload:
        raise SystemExit("read-back payload differs from the written smoke data")
    client.rm(f"{mount_path}smoke.bin")
finally:
    client.unmount(mount_path)
OPENVIKING_SMOKE
    then
        log "ERROR: the deployed OpenViking runtime failed the AGFS smoke cycle"
        return 40
    fi
    log "OpenViking AGFS runtime is ready for benchmarking"
}

run_openviking_benchmarks() {
    local raw_output
    local pipeline_status=0

    initialize_runtime || return $?
    if [[ ! -x "${OPENVIKING_PYTHON}" ]]; then
        log "ERROR: OpenViking is not deployed (run build first)"
        return 50
    fi
    raw_output="${RESULTS_DIR}/benchmark_openviking_raw.log"
    rm -f "${raw_output}"

    log "running the OpenViking AGFS context and micro benchmark suites"
    set +e
    "${OPENVIKING_PYTHON}" "${SCRIPT_DIR}/scripts/benchmark_context.py" \
        --results "${RESULTS_DIR}/benchmark_context.json" \
        --iterations "${OPENVIKING_ITERATIONS}" 2>&1 | tee -a "${raw_output}"
    pipeline_status="${PIPESTATUS[0]}"
    if [[ "${pipeline_status}" -eq 0 ]]; then
        "${OPENVIKING_PYTHON}" "${SCRIPT_DIR}/scripts/micro_benchmark.py" \
            --results "${RESULTS_DIR}/micro_benchmark.json" \
            --iterations "${OPENVIKING_ITERATIONS}" 2>&1 | tee -a "${raw_output}"
        pipeline_status="${PIPESTATUS[0]}"
    fi
    if [[ "${pipeline_status}" -eq 0 ]]; then
        "${OPENVIKING_PYTHON}" "${SCRIPT_DIR}/scripts/aggregate_results.py" \
            --results-dir "${RESULTS_DIR}" \
            --output "${RESULTS_DIR}/results.json" 2>&1 | tee -a "${raw_output}"
        pipeline_status="${PIPESTATUS[0]}"
    fi
    set -e
    if [[ "${pipeline_status}" -ne 0 ]]; then
        log "ERROR: OpenViking benchmark suite failed (see ${raw_output})"
        return 50
    fi
    if [[ ! -s "${RESULTS_DIR}/results.json" ]]; then
        log "ERROR: the benchmark suite produced no aggregate results"
        return 50
    fi
    log "OpenViking benchmark suite completed"
}

stop_openviking_runtime() {
    initialize_runtime || return $?
    if [[ ! -e "${OPENVIKING_VENV}" ]]; then
        log "no managed OpenViking environment; nothing to stop"
    else
        rm -rf -- "${OPENVIKING_VENV}"
        log "OpenViking runtime removed from ${OPENVIKING_VENV}"
    fi
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/openviking/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/openviking" ]]; then
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
        stop_openviking_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_openviking_standalone() {
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
        if build_openviking; then
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
        if start_openviking_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_openviking_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_openviking_runtime; then
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

Deploy OpenViking from the official PyPI wheel and run the AGFS context and
micro benchmark suites as a standalone performance evaluation.
Results default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       OpenViking version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  OPENVIKING_ITERATIONS, OPENVIKING_PACKAGE_INDEX
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
    run_openviking_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
