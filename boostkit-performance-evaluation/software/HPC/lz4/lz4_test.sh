#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.9.4}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
LZ4_SOURCE_URL="${LZ4_SOURCE_URL:-https://github.com/lz4/lz4.git}"
LZBENCH_SOURCE_URL="${LZBENCH_SOURCE_URL:-https://github.com/inikep/lzbench.git}"
LZBENCH_VERSION="v2.2"
SILESIA_LOCAL_PATH="${SILESIA_LOCAL_PATH:-/home/runner/software/lz4/silesia.tar}"
SILESIA_URL="https://wanos.co/assets/silesia.tar"
SILESIA_SHA256="ea122ed051dc7a6c58d2bb56bb05b34d9f1537c4dc9e71519142e2ca8cd6338d"
SOURCE_DIR=""
LZBENCH_DIR=""
BENCHMARK_BIN=""
SILESIA_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() { printf '[lz4] %s\n' "$*"; }

normalize_arch() {
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/lz4/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/lz4-source"
    LZBENCH_DIR="${PERF_WORK_DIR}/lzbench-source"
    BENCHMARK_BIN="${LZBENCH_DIR}/lzbench"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_commands() {
    local required package
    local packages=()

    for required in git python3 make cc c++ sed tee grep ldd curl sha256sum; do
        if command -v "${required}" >/dev/null 2>&1; then
            continue
        fi
        case "${required}" in
            git) package="git" ;;
            python3) package="python3" ;;
            make) package="make" ;;
            cc) package="gcc" ;;
            c++) package="gcc-c++" ;;
            sed) package="sed" ;;
            tee) package="coreutils" ;;
            grep) package="grep" ;;
            ldd) package="glibc-common" ;;
            curl) package="curl" ;;
            sha256sum) package="coreutils" ;;
        esac
        log "missing required LZ4 command: ${required}"
        packages+=("${package}")
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install LZ4 build prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing LZ4 build packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install LZ4 build prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install LZ4 build prerequisites"
        return 30
    fi

    for required in git python3 make cc c++ sed tee grep ldd curl sha256sum; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required LZ4 command remains unavailable: ${required}"
            return 30
        fi
    done
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

read_header_version() {
    local header="$1" major minor release
    major="$(sed -n 's/^#define LZ4_VERSION_MAJOR[[:space:]]\+\([0-9][0-9]*\).*/\1/p' "${header}")"
    minor="$(sed -n 's/^#define LZ4_VERSION_MINOR[[:space:]]\+\([0-9][0-9]*\).*/\1/p' "${header}")"
    release="$(
        sed -n 's/^#define LZ4_VERSION_RELEASE[[:space:]]\+\([0-9][0-9]*\).*/\1/p' "${header}"
    )"
    [[ -n "${major}" && -n "${minor}" && -n "${release}" ]] || return 1
    printf '%s.%s.%s\n' "${major}" "${minor}" "${release}"
}

build_lz4() {
    local tag actual_version
    initialize_runtime || return $?
    [[ "${SOFTWARE_VERSION}" == "1.9.4" ]] || {
        log "ERROR: this lzbench case supports only LZ4 1.9.4"
        return 10
    }
    check_architecture || return $?
    require_commands || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${LZBENCH_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }

    export GIT_TERMINAL_PROMPT=0
    tag="${SOFTWARE_VERSION}"
    [[ "${tag}" == v* ]] || tag="v${tag}"
    log "cloning LZ4 ${tag} from ${LZ4_SOURCE_URL}"
    git clone --branch "${tag}" --depth 1 "${LZ4_SOURCE_URL}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone LZ4 ${tag}"
        return 30
    }
    log "building the official LZ4 ${SOFTWARE_VERSION} shared library"
    make -C "${SOURCE_DIR}/lib" -j4 || {
        log "ERROR: failed to build the LZ4 shared library"
        return 40
    }
    [[ -f "${SOURCE_DIR}/lib/liblz4.so.1.9.4" ]] || {
        log "ERROR: LZ4 1.9.4 shared library was not created"
        return 40
    }

    actual_version="$(read_header_version "${SOURCE_DIR}/lib/lz4.h")" || {
        log "ERROR: cannot read the built LZ4 version"
        return 40
    }
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: source LZ4 version ${actual_version} differs from ${SOFTWARE_VERSION}"
        return 40
    }
    log "cloning lzbench ${LZBENCH_VERSION} from ${LZBENCH_SOURCE_URL}"
    git clone --branch "${LZBENCH_VERSION}" --depth 1 \
        "${LZBENCH_SOURCE_URL}" "${LZBENCH_DIR}" || {
        log "ERROR: failed to clone lzbench ${LZBENCH_VERSION}"
        return 30
    }
    local registry="${LZBENCH_DIR}/bench/lzbench.h"
    grep -Fq '"lz4 1.10.0"' "${registry}" || {
        log "ERROR: expected lzbench LZ4 display label was not found"
        return 40
    }
    sed -i '/{ "lz4",/s/"lz4 1.10.0"/"lz4 1.9.4"/' "${registry}"
    grep -Fq '"lz4 1.9.4"' "${registry}" || {
        log "ERROR: failed to label the linked LZ4 1.9.4 library"
        return 40
    }
    log "building lzbench against this run's LZ4 1.9.4 shared library"
    make -C "${LZBENCH_DIR}" -j4 BUILD_STATIC=0 DONT_BUILD_DENSITY=1 \
        LZ4_FILES= \
        USER_LDFLAGS="-L${SOURCE_DIR}/lib -Wl,-rpath,${SOURCE_DIR}/lib -llz4" || {
        log "ERROR: failed to build lzbench against LZ4 1.9.4"
        return 40
    }
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: lzbench executable was not created"
        return 40
    }
    ldd "${BENCHMARK_BIN}" | grep -Fq "=> ${SOURCE_DIR}/lib/liblz4.so.1 " || {
        log "ERROR: lzbench is not linked to this run's LZ4 shared library"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
}

start_lz4_runtime() {
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: lzbench executable is unavailable"
        return 40
    }
    ldd "${BENCHMARK_BIN}" | grep -Fq "=> ${SOURCE_DIR}/lib/liblz4.so.1 " || {
        log "ERROR: lzbench is not linked to this run's LZ4 shared library"
        return 40
    }
    log "LZ4 lzbench runtime is ready"
}

prepare_silesia_corpus() {
    if [[ -e "${SILESIA_LOCAL_PATH}" ]]; then
        [[ -r "${SILESIA_LOCAL_PATH}" ]] || {
            log "ERROR: local Silesia corpus is unreadable: ${SILESIA_LOCAL_PATH}"
            return 40
        }
        SILESIA_FILE="${SILESIA_LOCAL_PATH}"
        log "using local Silesia corpus ${SILESIA_FILE}"
    else
        SILESIA_FILE="${PERF_WORK_DIR}/silesia.tar"
        if [[ ! -f "${SILESIA_FILE}" ]]; then
            log "downloading Silesia corpus from ${SILESIA_URL}"
            curl -fL --retry 3 --connect-timeout 30 \
                -o "${SILESIA_FILE}.part" "${SILESIA_URL}" || {
                log "ERROR: failed to download the Silesia corpus"
                return 40
            }
            mv "${SILESIA_FILE}.part" "${SILESIA_FILE}" || return 40
        fi
    fi
    printf '%s  %s\n' "${SILESIA_SHA256}" "${SILESIA_FILE}" | sha256sum -c - || {
        log "ERROR: Silesia corpus checksum mismatch"
        return 40
    }
}

run_lz4_benchmarks() {
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: lzbench executable is unavailable"
        return 40
    }
    prepare_silesia_corpus || return 40
    export SOFTWARE_VERSION EXPECTED_ARCH
    python3 "${SCRIPT_DIR}/scripts/run_lzbench.py" \
        "${BENCHMARK_BIN}" "${SILESIA_FILE}" \
        "${RESULTS_DIR}/benchmark_lzbench.txt" \
        "${RESULTS_DIR}/benchmark_lz4.json" || return 50
}

stop_lz4_runtime() {
    log "LZ4 lzbench has no background service to stop"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/lz4/local-* && "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/lz4" ]] || {
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
        stop_lz4_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_lz4_standalone() {
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
        if build_lz4; then
            if ! standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_arch "${EXPECTED_ARCH}")" \
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
        if start_lz4_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if run_lz4_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_lz4_runtime; then
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
        "$(normalize_arch "${EXPECTED_ARCH}")" \
        "${PERF_RUN_ID}" \
        "${command_status}" \
        "${cleanup_status}" \
        "${failed_stage}"; then
        :
    else
        finalize_status=$?
    fi

    if [[ "${stage_status}" -ne 0 ]]; then
        trap - EXIT
        return "${stage_status}"
    fi
    if [[ "${cleanup_status}" != "passed" ]]; then
        trap - EXIT
        return 70
    fi
    trap - EXIT
    return "${finalize_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Build LZ4 1.9.4, prepare Silesia, run lzbench, collect the environment,
validate results, generate a report, and clean the isolated work directory.

Options:
  --version VERSION       LZ4 version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, LZ4_SOURCE_URL,
  LZBENCH_SOURCE_URL, SILESIA_LOCAL_PATH
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
    run_lz4_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
