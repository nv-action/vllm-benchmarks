#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.4.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
DATA_SCALE="${DATA_SCALE:-100K}"
DATA_DIM="${DATA_DIM:-128}"
ITERATIONS="${ITERATIONS:-3}"
K="${K:-10}"

SCANN_INSTALL_DIR=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0


log_message() {
    printf '[scann] %s\n' "$*"
}


normalize_architecture() {
    local architecture
    architecture="${1,,}"
    case "${architecture}" in
        x86_64|amd64)
            printf 'x86_64\n'
            ;;
        aarch64|arm64)
            printf 'aarch64\n'
            ;;
        *)
            printf '%s\n' "${architecture}"
            ;;
    esac
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log_message "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/scann/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi

    SCANN_INSTALL_DIR="${PERF_WORK_DIR}/scann-install"

    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR DATA_SCALE DATA_DIM ITERATIONS K
}


initialize_runtime() {
    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" \
        "${TMPDIR}"
}


require_build_commands() {
    local packages=() dnf_options=()
    if ! command -v python3 >/dev/null 2>&1; then
        packages+=(python3 python3-pip)
    elif ! python3 -m pip --version >/dev/null 2>&1; then
        packages+=(python3-pip)
    fi
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log_message "ERROR: dnf is required to install ScaNN build dependencies"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log_message "installing missing ScaNN build packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    command -v python3 >/dev/null 2>&1 && python3 -m pip --version >/dev/null 2>&1 || {
        log_message "ERROR: python3 pip module remains unavailable"
        return 30
    }
}


check_architecture() {
    local actual_architecture
    local expected_architecture
    actual_architecture="$(normalize_architecture "$(uname -m)")"
    expected_architecture="$(normalize_architecture "${EXPECTED_ARCH}")"
    if [[ "${actual_architecture}" != "${expected_architecture}" ]]; then
        log_message "ERROR: expected ${expected_architecture}, runner is ${actual_architecture}"
        return 20
    fi
}


operating_system_id() {
    local os_id
    os_id="$(sed -n 's/^ID=//p' /etc/os-release 2>/dev/null | head -n 1)"
    os_id="${os_id%\"}"
    os_id="${os_id#\"}"
    printf '%s\n' "${os_id,,}"
}


python_pip_index_options() {
    local os_id
    os_id="$(operating_system_id)"
    if [[ "${os_id}" != "ubuntu" ]]; then
        printf '%s\n' \
            '--trusted-host' 'mirrors.huaweicloud.com' \
            '--index-url' 'https://mirrors.huaweicloud.com/repository/pypi/simple'
    fi
}


install_scann_package() {
    local pip_index_options
    local pip_options
    pip_options=(
        --disable-pip-version-check
        --no-input
        --no-cache-dir
        --only-binary=:all:
        --target "${SCANN_INSTALL_DIR}"
    )
    mapfile -t pip_index_options < <(python_pip_index_options)
    log_message "installing official prebuilt scann==${SOFTWARE_VERSION} into the private work area"
    if ! python3 -m pip install "${pip_options[@]}" \
        "${pip_index_options[@]}" \
        "scann==${SOFTWARE_VERSION}"; then
        log_message "ERROR: failed to install scann==${SOFTWARE_VERSION}"
        return 30
    fi
}


activate_scann_runtime() {
    PYTHONPATH="${SCANN_INSTALL_DIR}${PYTHONPATH:+:${PYTHONPATH}}"
    export PYTHONPATH
    if ! python3 -c 'import scann, numpy'; then
        log_message "ERROR: installed ScaNN Python module cannot be imported"
        return 40
    fi
}


installed_scann_version() {
    PYTHONPATH="${SCANN_INSTALL_DIR}${PYTHONPATH:+:${PYTHONPATH}}" python3 -c '
import importlib.metadata
print(importlib.metadata.version("scann"))
'
}


build_scann() {
    local actual_version

    if initialize_runtime; then
        :
    else
        return $?
    fi
    if check_architecture; then
        :
    else
        return $?
    fi
    if require_build_commands; then
        :
    else
        return $?
    fi
    if [[ -e "${SCANN_INSTALL_DIR}" ]]; then
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi

    if install_scann_package; then
        :
    else
        return $?
    fi
    if activate_scann_runtime; then
        :
    else
        return $?
    fi
    actual_version="$(installed_scann_version)"
    if [[ -z "${actual_version}" ]]; then
        log_message "ERROR: cannot read the installed ScaNN version"
        return 40
    fi
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log_message "ERROR: installed ScaNN reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    if ! printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"; then
        log_message "ERROR: failed to record installed ScaNN version"
        return 40
    fi
    log_message "ScaNN ${actual_version} is ready (installed from the official PyPI wheel for ${EXPECTED_ARCH})"
}


start_scann_runtime() {
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if activate_scann_runtime; then
        :
    else
        return $?
    fi
    if ! python3 -c '
import numpy as np
import scann

rng = np.random.default_rng(0)
vectors = rng.random((32, 8), dtype="float32")
searcher = (
    scann.scann_ops_pybind.builder(vectors, 3, "dot_product")
    .tree(8, 1, training_sample_size=32, spherical=True, quantize_centroids=True)
    .score_ah(2, anisotropic_quantization_threshold=0.2)
    .reorder(3)
    .build()
)
neighbors, distances = searcher.search(vectors[0], 3, 3, 1)
assert neighbors.shape == (3,)
assert distances.shape == (3,)
'; then
        log_message "ERROR: ScaNN dot_product runtime validation failed"
        return 40
    fi
    log_message "ScaNN CPU Python runtime is ready"
}


run_scann_benchmarks() {
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if activate_scann_runtime; then
        :
    else
        return $?
    fi
    if ! python3 "${SCRIPT_DIR}/scripts/benchmark.py"; then
        log_message "ERROR: ScaNN benchmark failed"
        return 50
    fi
    log_message "ScaNN benchmark results written to benchmark.json"
}


stop_scann_runtime() {
    log_message "ScaNN benchmark has no background service to stop"
}


standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}


cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log_message "keeping standalone work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log_message "external work directory was not removed: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/scann/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/scann" ]]; then
        log_message "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        if ! rm -rf -- "${PERF_WORK_DIR}"; then
            log_message "ERROR: failed to clean standalone work directory"
            return 70
        fi
    fi
    log_message "cleaned standalone work directory: ${PERF_WORK_DIR}"
}


emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_scann_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}


run_scann_standalone() {
    local stage_status=0
    local failed_stage=""
    local cleanup_status="passed"
    local command_status="passed"
    local finalize_status=0

    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    STANDALONE_STOP_DONE=0
    STANDALONE_CLEANUP_DONE=0
    trap emergency_standalone_cleanup EXIT
    if initialize_runtime; then
        :
    else
        return $?
    fi

    if standalone_runtime system "${RESULTS_DIR}/system_info.json"; then
        if standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
            :
        else
            stage_status=$?
            failed_stage="prepare"
        fi
    else
        stage_status=$?
        failed_stage="prepare"
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if build_scann; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_architecture "${EXPECTED_ARCH}")" \
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
        if start_scann_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_scann_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_scann_runtime; then
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
        "$(normalize_architecture "${EXPECTED_ARCH}")" \
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

Install the official ScaNN wheel into a private work area, run the ScaNN vector
search benchmark, collect environment information, validate results, generate a
report, and clean the private work area.

Options:
  --version VERSION       ScaNN version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  DATA_SCALE, DATA_DIM, ITERATIONS, K
USAGE
}


main() {
    local pipeline_status
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                if [[ "$#" -lt 2 ]]; then
                    log_message "ERROR: --version requires a value"
                    return 10
                fi
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                if [[ "$#" -lt 2 ]]; then
                    log_message "ERROR: --results-dir requires a value"
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
                log_message "ERROR: unsupported option: $1"
                usage
                return 10
                ;;
        esac
    done

    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    mkdir -p "${RESULTS_DIR}"
    : > "${RESULTS_DIR}/results.log"
    pipeline_status=0
    set +e
    run_scann_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
