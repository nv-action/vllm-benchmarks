#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-0.8.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
HNSWLIB_SOURCE_URL="${HNSWLIB_SOURCE_URL:-https://github.com/nmslib/hnswlib.git}"
HNSWLIB_DATA_ROOT="${HNSWLIB_DATA_ROOT:-/home/runner/software/hnswlib/data}"
readonly SRA_SOURCE_URL="https://atomgit.com/liuliuyiyidingding/sra_test.git"
readonly SRA_REVISION="9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf"

SOURCE_DIR=""
SRA_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0


log() {
    printf '[hnswlib] %s\n' "$*"
}


configure_runtime_paths() {
    local actual_architecture

    if [[ "${SOFTWARE_VERSION}" != "0.8.0" ]]; then
        log "ERROR: only hnswlib 0.8.0 is supported, requested ${SOFTWARE_VERSION}"
        return 10
    fi
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64) EXPECTED_ARCH="x86_64" ;;
        aarch64|arm64) EXPECTED_ARCH="aarch64" ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) actual_architecture="x86_64" ;;
        aarch64|arm64) actual_architecture="aarch64" ;;
        *) actual_architecture="$(uname -m)" ;;
    esac
    if [[ "${actual_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected ${EXPECTED_ARCH}, runner is ${actual_architecture}"
        return 20
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/hnswlib/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    XDG_CACHE_HOME="${PERF_WORK_DIR}/cache"

    SOURCE_DIR="${PERF_WORK_DIR}/hnswlib-source"
    SRA_DIR="${PERF_WORK_DIR}/sra-test"

    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR HNSWLIB_DATA_ROOT
    export PERF_ACTUAL_VERSION_FILE TMPDIR XDG_CACHE_HOME
}


initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${XDG_CACHE_HOME}" \
        "${HNSWLIB_DATA_ROOT}" || return $?
}


require_hnswlib_tools() {
    local command_name package
    local packages=()

    for command_name in git g++ make curl h5dump python3; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            git) package="git" ;;
            g++) package="gcc-c++" ;;
            make) package="make" ;;
            curl) package="curl" ;;
            h5dump) package="hdf5" ;;
            python3) package="python3" ;;
        esac
        log "missing required hnswlib command: ${command_name}"
        packages+=("${package}")
    done
    [[ -f /usr/include/H5Cpp.h || -f /usr/include/hdf5/serial/H5Cpp.h ]] || packages+=(hdf5-devel)
    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install hnswlib prerequisites"
        return 30
    fi
    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing hnswlib packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install hnswlib prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install hnswlib prerequisites"
        return 30
    fi
    for command_name in git g++ make curl h5dump python3; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required hnswlib command remains unavailable: ${command_name}"
            return 30
        fi
    done
    if [[ ! -f /usr/include/H5Cpp.h && ! -f /usr/include/hdf5/serial/H5Cpp.h ]]; then
        log "ERROR: HDF5 C++ development headers remain unavailable"
        return 30
    fi
}


build_hnswlib() {
    local source_tag
    local actual_tag
    local config_file

    initialize_runtime || return $?
    require_hnswlib_tools || return $?
    if [[ -e "${SOURCE_DIR}" || -e "${SRA_DIR}" ]]; then
        log "ERROR: build directory is not clean under ${PERF_WORK_DIR}"
        return 20
    fi

    source_tag="${SOFTWARE_VERSION}"
    if [[ "${source_tag}" != v* ]]; then
        source_tag="v${source_tag}"
    fi
    export GIT_TERMINAL_PROMPT=0
    log "cloning hnswlib ${source_tag} from ${HNSWLIB_SOURCE_URL}"
    if ! git clone --branch "${source_tag}" --depth 1 \
        "${HNSWLIB_SOURCE_URL}" "${SOURCE_DIR}"; then
        log "ERROR: failed to clone hnswlib ${source_tag}"
        return 30
    fi
    actual_tag="$(git -C "${SOURCE_DIR}" describe --tags --exact-match HEAD)" || {
        log "ERROR: cloned source is not at an exact version tag"
        return 30
    }
    if [[ "${actual_tag}" != "${source_tag}" ]]; then
        log "ERROR: cloned source tag ${actual_tag}, expected ${source_tag}"
        return 30
    fi
    if [[ ! -f "${SOURCE_DIR}/hnswlib/hnswlib.h" ]]; then
        log "ERROR: hnswlib C++ header is unavailable"
        return 40
    fi

    log "fetching sra_test at ${SRA_REVISION}"
    if ! git init -q "${SRA_DIR}" || \
       ! git -C "${SRA_DIR}" remote add origin "${SRA_SOURCE_URL}" || \
       ! git -C "${SRA_DIR}" fetch --depth 1 --filter=blob:none origin "${SRA_REVISION}" || \
       ! git -C "${SRA_DIR}" sparse-checkout set scripts configs include src || \
       ! git -C "${SRA_DIR}" checkout --detach FETCH_HEAD; then
        log "ERROR: failed to fetch pinned sra_test source"
        return 30
    fi
    if [[ "$(git -C "${SRA_DIR}" rev-parse HEAD)" != "${SRA_REVISION}" ]]; then
        log "ERROR: sra_test revision mismatch"
        return 40
    fi
    mkdir -p "${SRA_DIR}/build"
    config_file="${SRA_DIR}/build/config_hnswlib.sh"
    printf 'export HNSWLIB_INC=%q\nexport EXTRA_DEFINES=""\n' \
        "${SOURCE_DIR}/hnswlib" > "${config_file}"
    log "building original sra_test hnswlib_test"
    if ! (cd "${SRA_DIR}" && printf '\n' | make hnswlib_test); then
        log "ERROR: sra_test hnswlib_test build failed"
        return 40
    fi
    if [[ ! -x "${SRA_DIR}/hnswlib_test" ]]; then
        log "ERROR: built hnswlib_test is unavailable"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    if ! printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"; then
        log "ERROR: failed to record built hnswlib version"
        return 40
    fi
    log "hnswlib ${SOFTWARE_VERSION} sra_test benchmark is ready"
}


start_hnswlib_runtime() {
    initialize_runtime || return $?
    if [[ ! -x "${SRA_DIR}/hnswlib_test" ]]; then
        log "ERROR: sra_test hnswlib_test is unavailable"
        return 40
    fi
    log "sra_test hnswlib_test is ready"
}


run_hnswlib_benchmarks() {
    local actual_version
    local benchmark_status

    initialize_runtime || return $?
    if [[ ! -f "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        log "ERROR: actual version file is missing"
        return 50
    fi
    actual_version="$(sed -n '1p' "${PERF_ACTUAL_VERSION_FILE}")" || return 50
    if [[ -e "${RESULTS_DIR}/benchmark_sra.json" ]]; then
        log "ERROR: hnswlib benchmark result already exists"
        return 50
    fi

    log "running original sra_test hnswlib_test on five datasets"
    if python3 "${SCRIPT_DIR}/scripts/run_sra_benchmark.py" \
        --source "${SRA_DIR}" \
        --data "${HNSWLIB_DATA_ROOT}" \
        --results "${RESULTS_DIR}" \
        --version "${actual_version}" \
        --architecture "${EXPECTED_ARCH}"; then
        :
    else
        benchmark_status=$?
        log "ERROR: sra_test hnswlib benchmark failed with status ${benchmark_status}"
        return "${benchmark_status}"
    fi
    log "hnswlib benchmark results written to benchmark_sra.json"
}


stop_hnswlib_runtime() {
    log "hnswlib benchmark has no background service to stop"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/hnswlib/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/hnswlib" ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        if ! rm -rf -- "${PERF_WORK_DIR}"; then
            log "ERROR: failed to clean standalone work directory"
            return 70
        fi
    fi
    log "cleaned standalone work directory: ${PERF_WORK_DIR}"
}


emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_hnswlib_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}


run_hnswlib_standalone() {
    local stage_status=0
    local failed_stage=""
    local cleanup_status="passed"
    local command_status="passed"
    local finalize_status=0

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
        if build_hnswlib; then
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
        if start_hnswlib_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_hnswlib_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_hnswlib_runtime; then
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
        :
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

Build the original sra_test hnswlib C++ benchmark from official hnswlib headers,
run five datasets, collect environment information, validate results, generate a
report, and clean the private work area.

Options:
  --version VERSION       hnswlib version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  HNSWLIB_SOURCE_URL, HNSWLIB_DATA_ROOT
USAGE
}


main() {
    local pipeline_status
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
    mkdir -p "${RESULTS_DIR}"
    : > "${RESULTS_DIR}/results.log"
    pipeline_status=0
    set +e
    run_hnswlib_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
