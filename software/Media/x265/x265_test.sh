#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-4.2}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
X265_ARCHIVE_BASE_URL="https://bitbucket.org/multicoreware/x265_git/downloads"
VIDEO_DIR="${VIDEO_DIR:-/home/runner/software/x265/video}"
SOURCE_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
BENCHMARK_BIN=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() { printf '[x265] %s\n' "$*"; }

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
        PERF_WORK_DIR="/home/runner/boostkit-perf/x265/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/x265_${SOFTWARE_VERSION}"
    BUILD_DIR="${SOURCE_DIR}/x265_build"
    INSTALL_DIR="${SOURCE_DIR}/x265_install"
    BENCHMARK_BIN="${INSTALL_DIR}/bin/x265"
    LD_LIBRARY_PATH="${INSTALL_DIR}/lib${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
    PATH="${INSTALL_DIR}/bin:${PATH}"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR LD_LIBRARY_PATH PATH
}

initialize_runtime() {
    configure_runtime_paths
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_commands() {
    local required package
    local packages=() dnf_options=()
    for required in python3 make cmake gcc g++ sed tee nproc wget tar taskset bc; do
        command -v "${required}" >/dev/null 2>&1 && continue
        case "${required}" in
            g++) package="gcc-c++" ;;
            tee) package="coreutils" ;;
            nproc) package="coreutils" ;;
            taskset) package="util-linux" ;;
            *) package="${required}" ;;
        esac
        packages+=("${package}")
    done
    if [[ "$(normalize_arch "${EXPECTED_ARCH}")" == "x86_64" ]] && ! command -v nasm >/dev/null 2>&1; then
        packages+=(nasm)
    fi
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log "ERROR: dnf is required to install x265 build dependencies"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log "installing missing x265 build packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    for required in python3 make cmake gcc g++ sed tee nproc wget tar taskset bc; do
        command -v "${required}" >/dev/null 2>&1 || {
            log "ERROR: required command remains unavailable: ${required}"
            return 30
        }
    done
    if [[ "$(normalize_arch "${EXPECTED_ARCH}")" == "x86_64" ]] && ! command -v nasm >/dev/null 2>&1; then
        log "ERROR: nasm remains unavailable"
        return 30
    fi
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

read_x265_version() {
    local version_output
    if [[ ! -x "${BENCHMARK_BIN}" ]]; then
        printf ''
        return 0
    fi

    if ! version_output="$("${BENCHMARK_BIN}" --version 2>&1)"; then
        printf ''
        return 0
    fi
    sed -n 's/^x265 \[info\]: HEVC encoder version \([^[:space:]]*\).*$/\1/p' <<<"${version_output}"
}

build_x265() {
    local actual_version archive_name
    initialize_runtime || return $?
    check_architecture || return $?
    require_commands || return $?
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }

    [[ "${SOFTWARE_VERSION}" == "4.2" ]] || {
        log "ERROR: the reference installation only defines x265 4.2"
        return 10
    }
    archive_name="x265_${SOFTWARE_VERSION}.tar.gz"
    log "downloading official x265 source archive ${archive_name}"
    (
        cd "${PERF_WORK_DIR}"
        wget "${X265_ARCHIVE_BASE_URL}/${archive_name}" || exit 30
        tar -zxvf "${archive_name}" || exit 30
    ) || return $?
    [[ -d "${SOURCE_DIR}/source" ]] || {
        log "ERROR: x265 source directory was not extracted: ${SOURCE_DIR}"
        return 30
    }

    log "building and installing x265 into ${INSTALL_DIR}"
    mkdir -p "${BUILD_DIR}" "${INSTALL_DIR}"
    (
        cd "${BUILD_DIR}"
        cmake -S ../source/ -DCMAKE_BUILD_TYPE=Release \
            -DENABLE_PIC=ON \
            -DENABLE_ASSEMBLY=ON \
            -DCMAKE_VERBOSE_MAKEFILE=ON \
            -DCMAKE_C_FLAGS="-O3" \
            -DCMAKE_CXX_FLAGS="-O3" \
            -DHIGH_BIT_DEPTH=off \
            -DENABLE_TESTS=on \
            -DENABLE_SHARED=on \
            -DENABLE_LIBNUMA=off \
            -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}" || {
            log "ERROR: x265 cmake configure failed"
            exit 40
        }
        make -j"$(nproc)" && make install || {
            log "ERROR: x265 make or install failed"
            exit 40
        }
    ) || return $?

    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official x265 binary was not created"
        return 40
    }

    actual_version="$(read_x265_version)"
    [[ -n "${actual_version}" ]] || {
        log "ERROR: cannot read the built x265 version"
        return 40
    }
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" || \
       "${actual_version}" == "${SOFTWARE_VERSION}"+* ]] || {
        log "ERROR: built x265 version does not match ${SOFTWARE_VERSION}: ${actual_version}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "x265 ${actual_version} private installation is ready"
}

start_x265_runtime() {
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official x265 binary is unavailable"
        return 40
    }
    "${BENCHMARK_BIN}" --version >/dev/null 2>&1 || {
        log "ERROR: x265 binary is not runnable"
        return 40
    }
    [[ -d "${VIDEO_DIR}" ]] || {
        log "ERROR: reference videos are unavailable: ${VIDEO_DIR}"
        return 40
    }
    python3 "${SCRIPT_DIR}/../collect_reference.py" \
        --check-only --video-dir "${VIDEO_DIR}" || return 40
    [[ ! -e "${PERF_WORK_DIR}/video" ]] || {
        log "ERROR: reference video link already exists: ${PERF_WORK_DIR}/video"
        return 40
    }
    ln -s "${VIDEO_DIR}" "${PERF_WORK_DIR}/video" || return 40
    log "x265 official benchmark runtime is ready"
}

run_x265_benchmarks() {
    local machine_type="x86"
    initialize_runtime || return $?
    [[ -x "${BENCHMARK_BIN}" ]] || {
        log "ERROR: official x265 binary is unavailable"
        return 40
    }
    [[ -d "${VIDEO_DIR}" ]] || {
        log "ERROR: reference videos are unavailable: ${VIDEO_DIR}"
        return 40
    }
    if [[ "$(normalize_arch "${EXPECTED_ARCH}")" == "aarch64" ]]; then
        machine_type="920"
    fi
    export TEST_HOME="${PERF_WORK_DIR}"
    (
        cd "${TEST_HOME}"
        bash "${SCRIPT_DIR}/scripts/fps-encode-x265.sh" 8core "${machine_type}" 1
    ) 2>&1 | tee "${RESULTS_DIR}/benchmark_encode.txt" || return 50
    (
        cd "${TEST_HOME}"
        bash "${SCRIPT_DIR}/scripts/score-x265.sh"
    ) 2>&1 | tee -a "${RESULTS_DIR}/benchmark_encode.txt" || return 50
    cp "${TEST_HOME}/video_fps_summary_x265.csv" \
        "${RESULTS_DIR}/video_fps_summary_x265.csv" || return 50
    python3 "${SCRIPT_DIR}/../collect_reference.py" \
        --encoder x265 --video-dir "${VIDEO_DIR}" --work-dir "${TEST_HOME}" \
        --raw-output "${RESULTS_DIR}/benchmark_encode.txt" \
        --summary "${RESULTS_DIR}/video_fps_summary_x265.csv" \
        --normalized-output "${RESULTS_DIR}/benchmark_x265.json" \
        --version "${SOFTWARE_VERSION}" \
        --architecture "$(normalize_arch "${EXPECTED_ARCH}")" || return 50
}

stop_x265_runtime() {
    log "x265 benchmark has no background service to stop"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/x265/local-* && \
       "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/x265" ]] || {
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
        stop_x265_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_x265_standalone() {
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
        if build_x265; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_arch "${EXPECTED_ARCH}")" \
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
        if start_x265_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_x265_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_x265_runtime; then
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

Build and run x265's official H.265/HEVC encode benchmark as a standalone
performance evaluation. Results default to results/<version>/<run-id>/ inside
this directory.

Options:
  --version VERSION       x265 version tag (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, VIDEO_DIR
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
    run_x265_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
