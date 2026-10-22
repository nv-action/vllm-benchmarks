#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-3.14.7}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
CPYTHON_SOURCE_URL="${CPYTHON_SOURCE_URL:-https://github.com/python/cpython.git}"
PYPI_INDEX_URL="https://mirrors.huaweicloud.com/repository/pypi/simple"
PYPI_TRUSTED_HOST="mirrors.huaweicloud.com"
# Keep the benchmark runner and warmup policy identical on both architectures.
PYPERFORMANCE_VERSION="1.13.0"
PYPERFORMANCE_WARMUP="3"
# Match the documented CPython performance-build configuration.
CONFIGURE_OPTIONS="--enable-optimizations --with-lto"

SOURCE_DIR=""
INSTALL_DIR=""
BENCH_WORK_DIR=""
PYTHON_BIN=""
GCC_VERSION_STRING=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log_message() { printf '[python] %s\n' "$*"; }

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
        log_message "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    elif [[ "${RESULTS_DIR}" != /* ]]; then
        RESULTS_DIR="$(pwd -P)/${RESULTS_DIR}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/python/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/cpython-source"
    INSTALL_DIR="${PERF_WORK_DIR}/cpython-install"
    # pyperformance creates its benchmark venvs under ./venv relative to the
    # working directory, so run it from here to keep them inside the work area.
    BENCH_WORK_DIR="${PERF_WORK_DIR}/pyperformance-work"
    PYTHON_BIN="${INSTALL_DIR}/bin/python3"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_python_tools() {
    local command_name package
    local packages=()

    for command_name in git gcc make python3 nproc sudo; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            git) package="git" ;;
            gcc) package="gcc" ;;
            make) package="make" ;;
            python3) package="python3" ;;
            nproc) package="coreutils" ;;
            sudo) package="sudo" ;;
        esac
        log_message "missing required Python test command: ${command_name}"
        packages+=("${package}")
    done

    # These headers build the CPython modules verified before pyperformance
    # runs: _ssl, zlib and _ctypes.  The remaining headers keep the standard
    # library feature-complete for the complete official benchmark suite.
    for package in bzip2-devel gdbm-devel libffi-devel openssl-devel readline-devel sqlite-devel uuid-devel xz-devel zlib-devel; do
        if ! rpm -q "${package}" >/dev/null 2>&1; then
            log_message "missing required Python test package: ${package}"
            packages+=("${package}")
        fi
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log_message "ERROR: dnf is required to install Python test prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log_message "installing missing Python test packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log_message "ERROR: sudo is required to install Python test prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log_message "ERROR: failed to install Python test prerequisites"
        return 30
    fi

    for command_name in git gcc make python3 nproc sudo; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log_message "ERROR: required Python test command remains unavailable: ${command_name}"
            return 30
        fi
    done
    for package in bzip2-devel gdbm-devel libffi-devel openssl-devel readline-devel sqlite-devel uuid-devel xz-devel zlib-devel; do
        if ! rpm -q "${package}" >/dev/null 2>&1; then
            log_message "ERROR: required Python test package remains unavailable: ${package}"
            return 30
        fi
    done
}

check_architecture() {
    local actual expected
    actual="$(normalize_architecture "$(uname -m)")"
    expected="$(normalize_architecture "${EXPECTED_ARCH}")"
    [[ "${actual}" == "${expected}" ]] || {
        log_message "ERROR: expected architecture ${expected}, runner is ${actual}"
        return 20
    }
}

prepare_cpython_source() {
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log_message "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 30
    }
    export GIT_TERMINAL_PROMPT=0
    log_message "cloning CPython v${SOFTWARE_VERSION} from ${CPYTHON_SOURCE_URL}"
    git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${CPYTHON_SOURCE_URL}" "${SOURCE_DIR}" || {
        log_message "ERROR: failed to clone CPython v${SOFTWARE_VERSION}"
        return 30
    }
}

verify_cpython_build() {
    local version_output actual_version
    [[ -x "${PYTHON_BIN}" ]] || {
        log_message "ERROR: CPython interpreter was not installed: ${PYTHON_BIN}"
        return 40
    }
    version_output="$("${PYTHON_BIN}" --version 2>&1)" || {
        log_message "ERROR: built CPython cannot report its version"
        return 40
    }
    actual_version="${version_output#Python }"
    actual_version="${actual_version%%[[:space:]]*}"
    [[ -n "${actual_version}" && "${actual_version}" != "${version_output}" ]] || {
        log_message "ERROR: unexpected CPython version output: ${version_output}"
        return 40
    }
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log_message "ERROR: built CPython reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40

    # pip needs ssl/zlib to download pyperformance; psutil needs ctypes.
    "${PYTHON_BIN}" -c "import ssl, zlib, ctypes" || {
        log_message "ERROR: built CPython is missing ssl/zlib/ctypes modules"
        log_message "the Runner is missing zlib-devel or libffi-devel"
        return 40
    }
    "${PYTHON_BIN}" -m pip --version >/dev/null 2>&1 || {
        log_message "ERROR: built CPython has no usable pip"
        return 40
    }
}

build_python() {
    local -a configure_options

    initialize_runtime || return $?
    check_architecture || return $?
    require_python_tools || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${INSTALL_DIR}" && ! -e "${BENCH_WORK_DIR}" ]] || {
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    prepare_cpython_source || return $?
    GCC_VERSION_STRING="$(gcc --version | head -n 1)"
    read -r -a configure_options <<< "${CONFIGURE_OPTIONS}"
    [[ "${#configure_options[@]}" -gt 0 ]] || {
        log_message "ERROR: CPython configure options are empty"
        return 40
    }

    log_message "configuring CPython v${SOFTWARE_VERSION} with ${CONFIGURE_OPTIONS} into a private prefix"
    (
        cd "${SOURCE_DIR}"
        ./configure --prefix="${INSTALL_DIR}" "${configure_options[@]}"
    ) || {
        log_message "ERROR: CPython configure failed"
        return 40
    }
    log_message "building CPython v${SOFTWARE_VERSION} with make -j$(nproc)"
    (
        cd "${SOURCE_DIR}"
        make -j"$(nproc)"
    ) || {
        log_message "ERROR: CPython make failed"
        return 40
    }
    (
        cd "${SOURCE_DIR}"
        make install
    ) || {
        log_message "ERROR: CPython make install failed"
        return 40
    }
    verify_cpython_build || return $?
    log_message "CPython ${SOFTWARE_VERSION} interpreter is ready at ${PYTHON_BIN}"
}

start_python_runtime() {
    initialize_runtime || return $?
    [[ -x "${PYTHON_BIN}" ]] || {
        log_message "ERROR: CPython interpreter is unavailable: ${PYTHON_BIN}"
        return 40
    }
    "${PYTHON_BIN}" --version
    "${PYTHON_BIN}" -c "import ssl, zlib, ctypes" || {
        log_message "ERROR: CPython interpreter is missing ssl/zlib/ctypes modules"
        return 40
    }
    "${PYTHON_BIN}" -m pip --version || {
        log_message "ERROR: CPython interpreter has no usable pip"
        return 40
    }
    log_message "CPython official benchmark runtime is ready"
}

run_python_benchmarks() {
    initialize_runtime || return $?
    [[ -x "${PYTHON_BIN}" ]] || {
        log_message "ERROR: CPython interpreter is unavailable: ${PYTHON_BIN}"
        return 40
    }
    mkdir -p "${BENCH_WORK_DIR}" "${RESULTS_DIR}"
    log_message "installing pyperformance ${PYPERFORMANCE_VERSION} into the private CPython"
    log_message "running the official pyperformance default benchmark suite"
    (
        cd "${BENCH_WORK_DIR}"
        export PIP_NO_CACHE_DIR=1
        export PIP_DISABLE_PIP_VERSION_CHECK=1
        # pyperformance creates benchmark-specific virtual environments. The
        # 2to3 benchmark installs its bundled compatibility vendor there on
        # Python 3.14, so it must inherit the package source and network
        # proxy configuration from this process.
        export PIP_INDEX_URL="${PYPI_INDEX_URL}"
        export PIP_TRUSTED_HOST="${PYPI_TRUSTED_HOST}"
        "${PYTHON_BIN}" -m pip install --no-cache-dir \
            --index-url "${PYPI_INDEX_URL}" \
            --trusted-host "${PYPI_TRUSTED_HOST}" \
            "pyperformance==${PYPERFORMANCE_VERSION}" || exit 50
        "${PYTHON_BIN}" -m pyperformance run \
            --warmup "${PYPERFORMANCE_WARMUP}" \
            --inherit-environ \
                PIP_INDEX_URL,PIP_TRUSTED_HOST,http_proxy,https_proxy,no_proxy,all_proxy,HTTP_PROXY,HTTPS_PROXY,NO_PROXY,ALL_PROXY \
            -o "${RESULTS_DIR}/benchmark.json" || exit 50
    ) || {
        log_message "ERROR: official pyperformance run failed"
        return 50
    }
    [[ -s "${RESULTS_DIR}/benchmark.json" ]] || {
        log_message "ERROR: official pyperformance output is empty: ${RESULTS_DIR}/benchmark.json"
        return 50
    }
    export SOFTWARE_VERSION EXPECTED_ARCH PYPERFORMANCE_VERSION
    export PYPERFORMANCE_WARMUP CONFIGURE_OPTIONS
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/benchmark.json" \
        "${RESULTS_DIR}/benchmark_python.json" || {
        log_message "ERROR: failed to normalize official pyperformance results"
        return 50
    }
    log_message "pyperformance results written to benchmark.json and benchmark_python.json"
}

stop_python_runtime() {
    log_message "python benchmark has no background service to stop"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/python/local-* && \
       "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/python" ]] || {
        log_message "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        rm -rf -- "${PERF_WORK_DIR}" || return 70
    fi
    log_message "cleaned standalone work directory: ${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_python_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_python_standalone() {
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
        if build_python; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_architecture "${EXPECTED_ARCH}")" \
                "${PERF_RUN_ID}" \
                "${GCC_VERSION_STRING}" \
                --configure-options="${CONFIGURE_OPTIONS}"; then
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
        if start_python_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_python_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_python_runtime; then
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

Build CPython from source and run the official pyperformance benchmarks as a
standalone performance evaluation. Results default to
results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       CPython version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, CPYTHON_SOURCE_URL
USAGE
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                [[ "$#" -ge 2 ]] || { log_message "ERROR: --version requires a value"; return 10; }
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                [[ "$#" -ge 2 ]] || { log_message "ERROR: --results-dir requires a value"; return 10; }
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

    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    : > "${RESULTS_DIR}/results.log"
    local pipeline_status=0
    set +e
    run_python_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
