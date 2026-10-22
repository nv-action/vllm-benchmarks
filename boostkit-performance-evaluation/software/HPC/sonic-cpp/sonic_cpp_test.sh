#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-69deb02}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
SONIC_CPP_SOURCE_URL="${SONIC_CPP_SOURCE_URL:-https://github.com/bytedance/sonic-cpp.git}"
SONIC_CPP_REVISION="69deb02c099361139dd387eb6c7034a4836ce9b3"
BENCHMARK_MIN_TIME="3s"

SOURCE_DIR=""
BUILD_DIR=""
BENCHMARK_BIN=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() { printf '[sonic-cpp] %s\n' "$*"; }

normalize_architecture() {
    case "${1,,}" in
        x86_64|amd64) printf 'x86_64\n' ;;
        aarch64|arm64) printf 'aarch64\n' ;;
        *) printf '%s\n' "${1,,}" ;;
    esac
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/sonic-cpp/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/sonic-cpp-source"
    BUILD_DIR="${PERF_WORK_DIR}/build"
    BENCHMARK_BIN="${BUILD_DIR}/benchmark/bench"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_commands() {
    local required package
    local packages=() dnf_options=()
    for required in git python3 cmake make c++ tee; do
        command -v "${required}" >/dev/null 2>&1 && continue
        case "${required}" in
            tee) package="coreutils" ;;
            c++) package="gcc-c++" ;;
            *) package="${required}" ;;
        esac
        packages+=("${package}")
    done
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log "ERROR: dnf is required to install Sonic-cpp build dependencies"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log "installing missing Sonic-cpp build packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    for required in git python3 cmake make c++ tee; do
        command -v "${required}" >/dev/null 2>&1 || {
            log "ERROR: required command remains unavailable: ${required}"
            return 30
        }
    done
}

check_architecture() {
    local actual expected
    actual="$(normalize_architecture "$(uname -m)")"
    expected="$(normalize_architecture "${EXPECTED_ARCH}")"
    [[ "${actual}" == "${expected}" ]] || {
        log "ERROR: expected architecture ${expected}, runner is ${actual}"
        return 20
    }
}

prepare_sonic_cpp_source() {
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 30
    }
    export GIT_TERMINAL_PROMPT=0
    [[ "${SOFTWARE_VERSION}" == "69deb02" ]] || {
        log "ERROR: supported sonic-cpp source revision is 69deb02, got ${SOFTWARE_VERSION}"
        return 10
    }
    log "cloning sonic-cpp from ${SONIC_CPP_SOURCE_URL}"
    git clone "${SONIC_CPP_SOURCE_URL}" "${SOURCE_DIR}" &&
        git -C "${SOURCE_DIR}" checkout --detach "${SONIC_CPP_REVISION}" || {
        log "ERROR: failed to check out sonic-cpp revision ${SOFTWARE_VERSION}"
        return 30
    }
}

report_actual_version() {
    local actual_revision
    actual_revision="$(git -C "${SOURCE_DIR}" rev-parse HEAD)" || return 40
    [[ "${actual_revision}" == "${SONIC_CPP_REVISION}" ]] || {
        log "ERROR: cloned source revision '${actual_revision}' does not match ${SONIC_CPP_REVISION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "verified source commit: ${actual_revision}"
}

build_sonic_cpp() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_commands || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    prepare_sonic_cpp_source || return $?
    report_actual_version || return $?

    log "building official CMake benchmark target: bench"
    (
        cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" -DBUILD_BENCH=ON
        cmake --build "${BUILD_DIR}" --target bench -j
    ) || {
        log "ERROR: official CMake build of benchmark target failed"
        return 40
    }
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official benchmark binary was not produced: ${BENCHMARK_BIN}"
        return 40
    }
    log "sonic-cpp ${SOFTWARE_VERSION} official benchmark artifact is ready"
}

start_sonic_cpp_runtime() {
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official sonic-cpp benchmark binary is unavailable: ${BENCHMARK_BIN}"
        return 40
    }
    log "sonic-cpp official benchmark runtime is ready"
}

run_sonic_cpp_benchmarks() {
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official sonic-cpp benchmark binary is unavailable: ${BENCHMARK_BIN}"
        return 40
    }
    log "running official CMake benchmark (benchmark/main.cpp)"
    (
        # The binary loads testdata/ relative to the working directory.
        cd "${SOURCE_DIR}"
        "${BENCHMARK_BIN}" \
            "--benchmark_out_format=json" \
            "--benchmark_out=${RESULTS_DIR}/benchmark.json" \
            "--benchmark_filter=Sonic" \
            "--benchmark_min_time=${BENCHMARK_MIN_TIME}"
    ) || {
        log "ERROR: official sonic-cpp benchmark failed"
        return 50
    }
    export SOFTWARE_VERSION EXPECTED_ARCH BENCHMARK_MIN_TIME
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/benchmark.json" \
        "${RESULTS_DIR}/benchmark_sonic_cpp.json" || {
        log "ERROR: failed to normalize official sonic-cpp benchmark results"
        return 50
    }
    log "sonic-cpp benchmark results written to benchmark.json and benchmark_sonic_cpp.json"
}

stop_sonic_cpp_runtime() {
    log "sonic-cpp benchmark has no background service to stop"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/sonic-cpp/local-* && \
       "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/sonic-cpp" ]] || {
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        rm -rf -- "${PERF_WORK_DIR}" || return 70
    fi
    log "cleaned standalone work directory: ${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_sonic_cpp_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_sonic_cpp_standalone() {
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
        if build_sonic_cpp; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_architecture "${EXPECTED_ARCH}")" \
                "${PERF_RUN_ID}" \
                "${COMPILER_BINARY}" \
                "${COMPILER_VERSION_STRING}"; then
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
        if start_sonic_cpp_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_sonic_cpp_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_sonic_cpp_runtime; then
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
        finalize_status=0
    else
        finalize_status=$?
    fi
    trap - EXIT
    [[ "${stage_status}" -eq 0 ]] || return "${stage_status}"
    [[ "${cleanup_status}" == "passed" ]] || return 70
    return "${finalize_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Build and run sonic-cpp's official CMake benchmark (benchmark/main.cpp) as a
standalone performance evaluation. Results default to
results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       sonic-cpp source revision (fixed: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, SONIC_CPP_SOURCE_URL
USAGE
}

main() {
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
    run_sonic_cpp_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
