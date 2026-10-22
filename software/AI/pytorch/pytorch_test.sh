#!/usr/bin/env bash
# PyTorch performance case (official prebuilt CPU wheel deployment).
#
# The software under test is the official PyTorch CPU wheel for the requested
# release, installed into a per-run virtual environment inside this case's
# isolated work directory — the distribution package manager only ships one
# torch build and cannot switch between releases or pin the exact CPU flavor.
#
# The four framework stages map to: create a venv and install the pinned
# official wheel (build), verify the deployed torch runtime with a smoke
# operator (start), run the torch.utils.benchmark operator suite (test), and
# remove the venv (stop).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_NAME="pytorch"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2.13.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

# Fixed sources for the official prebuilt wheel and its Python dependencies.
# Dependencies resolve through the Huawei PyPI mirror because domestic
# runners cannot reliably reach files.pythonhosted.org.
PYTORCH_REPOSITORY="${PYTORCH_REPOSITORY:-https://github.com/pytorch/pytorch}"
PYTORCH_WHEEL_INDEX="${PYTORCH_WHEEL_INDEX:-https://download.pytorch.org/whl/cpu}"
PYTORCH_DEPENDENCY_INDEX="${PYTORCH_DEPENDENCY_INDEX:-https://mirrors.huaweicloud.com/repository/pypi/simple}"

# Thread levels for the benchmark suite (space-separated, overridable).
TORCH_THREAD_LEVELS="${TORCH_THREAD_LEVELS:-1 4 16}"

# Lifecycle paths (assigned in configure_runtime_paths).
PYTORCH_VENV=""
PYTORCH_PYTHON=""

log() {
    printf '[pytorch] %s\n' "$*"
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/pytorch/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    # The framework pre-creates ${PERF_WORK_DIR}/venv before every stage, so
    # the task-private environment must live under a different name.
    PYTORCH_VENV="${PERF_WORK_DIR}/torch-venv"
    PYTORCH_PYTHON="${PYTORCH_VENV}/bin/python"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TORCH_THREAD_LEVELS TMPDIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    if [[ ! "${TORCH_THREAD_LEVELS}" =~ ^[0-9]+( [0-9]+)*$ ]]; then
        log "ERROR: TORCH_THREAD_LEVELS must be space-separated thread counts: ${TORCH_THREAD_LEVELS}"
        return 10
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_pytorch_tools() {
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
        log "missing required PyTorch test command: ${command_name}"
        packages+=("${package}")
    done
    if ! rpm -q python3-pip >/dev/null 2>&1; then
        log "missing required PyTorch test package: python3-pip"
        packages+=("python3-pip")
    fi

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install PyTorch test prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing PyTorch test packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install PyTorch test prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install PyTorch test prerequisites"
        return 30
    fi

    for command_name in python3 curl sha256sum tee sudo; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required PyTorch test command remains unavailable: ${command_name}"
            return 30
        fi
    done
    if ! rpm -q python3-pip >/dev/null 2>&1; then
        log "ERROR: required PyTorch test package remains unavailable: python3-pip"
        return 30
    fi
}

# The official CPU wheels only exist for the interpreter versions declared in
# the release support matrix.  Adding a Python version or a torch release means
# updating this declaration; wheel resolution fails loudly otherwise.
supported_python_version() {
    local python_version
    python_version="$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])')" || return 30
    case "${python_version}" in
        3.10|3.11|3.12|3.13|3.14|3.15)
            printf '%s\n' "${python_version}"
            ;;
        *)
            log "ERROR: system python3 is ${python_version}, but the official PyTorch ${SOFTWARE_VERSION} CPU wheels require one of 3.10-3.15"
            return 30
            ;;
    esac
}

build_pytorch() {
    local wheel_version
    local actual_version
    local python_version
    local runner_architecture

    initialize_runtime || return $?
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    require_pytorch_tools || return $?
    if [[ -e "${PYTORCH_VENV}" ]]; then
        log "ERROR: build output is not clean under ${PYTORCH_VENV}"
        return 20
    fi

    python_version="$(supported_python_version)" || return $?
    log "system python3 is ${python_version}"
    log "creating the task-private virtual environment ${PYTORCH_VENV}"
    if ! python3 -m venv "${PYTORCH_VENV}"; then
        log "ERROR: failed to create the virtual environment"
        return 40
    fi
    if [[ ! -x "${PYTORCH_PYTHON}" || ! -x "${PYTORCH_VENV}/bin/pip" ]]; then
        log "ERROR: the virtual environment has no usable python/pip"
        return 40
    fi

    log "installing the official PyTorch ${SOFTWARE_VERSION} CPU wheel from ${PYTORCH_WHEEL_INDEX}"
    if ! "${PYTORCH_VENV}/bin/pip" install --no-cache-dir \
            --trusted-host mirrors.huaweicloud.com \
            --index-url "${PYTORCH_WHEEL_INDEX}" \
            --extra-index-url "${PYTORCH_DEPENDENCY_INDEX}" \
            "torch==${SOFTWARE_VERSION}+cpu"; then
        log "ERROR: failed to install torch==${SOFTWARE_VERSION}+cpu from the official wheel index"
        return 40
    fi

    wheel_version="$("${PYTORCH_PYTHON}" -c 'import torch; print(torch.__version__)' 2>/dev/null)" || {
        log "ERROR: the installed torch package cannot be imported"
        return 40
    }
    if [[ "${wheel_version}" != "${SOFTWARE_VERSION}+cpu" ]]; then
        log "ERROR: installed torch is ${wheel_version}, expected ${SOFTWARE_VERSION}+cpu"
        return 40
    fi
    actual_version="${wheel_version%%+*}"
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "torch ${wheel_version} deployed from the official CPU wheel"
}

start_pytorch_runtime() {
    initialize_runtime || return $?
    if [[ ! -x "${PYTORCH_PYTHON}" ]]; then
        log "ERROR: PyTorch is not deployed (run build first)"
        return 40
    fi

    if ! "${PYTORCH_PYTHON}" - <<'PYTORCH_SMOKE' >/dev/null 2>&1
import math
import torch

torch.manual_seed(0)
a = torch.randn(256, 256)
b = a @ a
if not torch.isfinite(b).all():
    raise SystemExit("matmul produced non-finite values")
PYTORCH_SMOKE
    then
        log "ERROR: the deployed torch runtime failed the smoke operator"
        return 40
    fi
    log "PyTorch runtime is ready for benchmarking"
}

run_pytorch_benchmarks() {
    local raw_output
    local pipeline_status=0

    initialize_runtime || return $?
    if [[ ! -x "${PYTORCH_PYTHON}" ]]; then
        log "ERROR: PyTorch is not deployed (run build first)"
        return 50
    fi
    raw_output="${RESULTS_DIR}/benchmark_torch_raw.log"
    rm -f "${raw_output}"

    log "running the torch.utils.benchmark operator suite"
    set +e
    "${PYTORCH_PYTHON}" "${SCRIPT_DIR}/scripts/run_torch_benchmark.py" \
        --threads "${TORCH_THREAD_LEVELS}" \
        --results "${RESULTS_DIR}/results.json" 2>&1 | tee "${raw_output}"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    if [[ "${pipeline_status}" -ne 0 ]]; then
        log "ERROR: torch benchmark suite failed (see ${raw_output})"
        return 50
    fi
    if [[ ! -s "${RESULTS_DIR}/results.json" ]]; then
        log "ERROR: the benchmark suite produced no aggregate results"
        return 50
    fi
    log "torch benchmark suite completed"
}

stop_pytorch_runtime() {
    initialize_runtime || return $?
    if [[ ! -e "${PYTORCH_VENV}" ]]; then
        log "no managed PyTorch environment; nothing to stop"
    else
        rm -rf -- "${PYTORCH_VENV}"
        log "PyTorch runtime removed from ${PYTORCH_VENV}"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/pytorch/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/pytorch" ]]; then
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
        stop_pytorch_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_pytorch_standalone() {
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
        if build_pytorch; then
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
        if start_pytorch_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_pytorch_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_pytorch_runtime; then
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

Deploy PyTorch from the official CPU wheel and run the torch.utils.benchmark
operator suite as a standalone performance evaluation.
Results default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       PyTorch version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  TORCH_THREAD_LEVELS, PYTORCH_WHEEL_INDEX, PYTORCH_DEPENDENCY_INDEX
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
    run_pytorch_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
