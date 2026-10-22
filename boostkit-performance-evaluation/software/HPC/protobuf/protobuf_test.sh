#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-33.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
PROTOBUF_SOURCE_URL="${PROTOBUF_SOURCE_URL:-https://github.com/protocolbuffers/protobuf.git}"
BENCHMARK_SOURCE_URL="https://gitcode.com/boostkit/AccLibBenchmark.git"
BENCHMARK_SOURCE_REVISION="01d083c6633babb2fb1307d93f596f10dda36778"
GOOGLE_BENCHMARK_URL="https://github.com/google/benchmark.git"
GOOGLE_BENCHMARK_VERSION="v1.8.3"

SOURCE_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
PROTOC_BIN=""
BENCHMARK_REPO_DIR=""
BENCHMARK_SOURCE_DIR=""
BENCHMARK_BUILD_DIR=""
BENCHMARK_BIN=""
FIXTURE_DUMP_BIN=""
GOOGLE_BENCHMARK_SOURCE_DIR=""
GOOGLE_BENCHMARK_BUILD_DIR=""
GOOGLE_BENCHMARK_INSTALL_DIR=""
CXX_BINARY="${CXX:-g++}"
CXX_VERSION=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log_message() {
    printf '[protobuf] %s\n' "$*"
}

normalized_architecture() {
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
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log_message "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/protobuf/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/protobuf-source"
    BUILD_DIR="${PERF_WORK_DIR}/build"
    INSTALL_DIR="${PERF_WORK_DIR}/install"
    PROTOC_BIN="${INSTALL_DIR}/bin/protoc"
    BENCHMARK_REPO_DIR="${PERF_WORK_DIR}/AccLibBenchmark"
    BENCHMARK_SOURCE_DIR="${BENCHMARK_REPO_DIR}/protobuf-benchmark"
    BENCHMARK_BUILD_DIR="${PERF_WORK_DIR}/benchmark-build"
    BENCHMARK_BIN="${BENCHMARK_BUILD_DIR}/bm"
    FIXTURE_DUMP_BIN="${BENCHMARK_BUILD_DIR}/protobuf_fixture_dump"
    GOOGLE_BENCHMARK_SOURCE_DIR="${PERF_WORK_DIR}/google-benchmark-source"
    GOOGLE_BENCHMARK_BUILD_DIR="${PERF_WORK_DIR}/google-benchmark-build"
    GOOGLE_BENCHMARK_INSTALL_DIR="${PERF_WORK_DIR}/google-benchmark-install"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${PERF_WORK_DIR}/tmp"
    export TMPDIR="${PERF_WORK_DIR}/tmp"
}

require_build_commands() {
    local required_command package
    local packages=() dnf_options=()
    for required_command in git cmake make nproc sha256sum cmp python3 "${CXX_BINARY}"; do
        command -v "${required_command}" >/dev/null 2>&1 && continue
        case "${required_command}" in
            nproc|sha256sum|cmp) package="coreutils" ;;
            g++|c++) package="gcc-c++" ;;
            clang++) package="clang" ;;
            /*)
                log_message "ERROR: custom C++ compiler is unavailable: ${required_command}"
                return 30
                ;;
            *) package="${required_command}" ;;
        esac
        packages+=("${package}")
    done
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log_message "ERROR: dnf is required to install Protobuf build dependencies"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log_message "installing missing Protobuf build packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    for required_command in git cmake make nproc sha256sum cmp python3 "${CXX_BINARY}"; do
        command -v "${required_command}" >/dev/null 2>&1 || {
            log_message "ERROR: required command remains unavailable: ${required_command}"
            return 30
        }
    done
}

check_architecture() {
    local actual_architecture expected_architecture
    actual_architecture="$(normalized_architecture "$(uname -m)")"
    expected_architecture="$(normalized_architecture "${EXPECTED_ARCH}")"
    if [[ "${actual_architecture}" != "${expected_architecture}" ]]; then
        log_message "ERROR: expected architecture ${expected_architecture}, runner is ${actual_architecture}"
        return 20
    fi
}

prepare_benchmark_source() {
    local local_source="${PROTOBUF_BENCHMARK_REPO:-/home/runner/software/protobuf/AccLibBenchmark}"
    if [[ -d "${local_source}/.git" ]]; then
        log_message "using local AccLibBenchmark repository ${local_source}"
        git clone --quiet --no-checkout "${local_source}" "${BENCHMARK_REPO_DIR}" || return 40
    else
        log_message "cloning AccLibBenchmark from ${BENCHMARK_SOURCE_URL}"
        GIT_TERMINAL_PROMPT=0 git clone --quiet --no-checkout \
            "${BENCHMARK_SOURCE_URL}" "${BENCHMARK_REPO_DIR}" || return 40
    fi
    git -C "${BENCHMARK_REPO_DIR}" checkout --quiet --detach \
        "${BENCHMARK_SOURCE_REVISION}" || return 40
    [[ -f "${BENCHMARK_SOURCE_DIR}/benchmark_main.cpp" ]] || {
        log_message "ERROR: pinned protobuf-benchmark source is missing"
        return 40
    }
    python3 "${SCRIPT_DIR}/scripts/fix_fixtures.py" \
        "${BENCHMARK_SOURCE_DIR}/benchmark_common.h" || return 40
    git -C "${BENCHMARK_REPO_DIR}" diff -- protobuf-benchmark/benchmark_common.h \
        > "${RESULTS_DIR}/benchmark_source.diff"
    [[ -s "${RESULTS_DIR}/benchmark_source.diff" ]] || {
        log_message "ERROR: fixed-fixture source diff is empty"
        return 40
    }
}

build_google_benchmark() {
    log_message "building Google Benchmark ${GOOGLE_BENCHMARK_VERSION}"
    GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 \
        --branch "${GOOGLE_BENCHMARK_VERSION}" \
        "${GOOGLE_BENCHMARK_URL}" "${GOOGLE_BENCHMARK_SOURCE_DIR}" || return 40
    cmake -S "${GOOGLE_BENCHMARK_SOURCE_DIR}" -B "${GOOGLE_BENCHMARK_BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release -DBENCHMARK_ENABLE_TESTING=OFF \
        -DBUILD_SHARED_LIBS=OFF -DCMAKE_INSTALL_LIBDIR=lib \
        -DCMAKE_INSTALL_PREFIX="${GOOGLE_BENCHMARK_INSTALL_DIR}" || return 40
    cmake --build "${GOOGLE_BENCHMARK_BUILD_DIR}" --parallel "$(nproc)" || return 40
    cmake --install "${GOOGLE_BENCHMARK_BUILD_DIR}" || return 40
}

clone_protobuf_source() {
    if [[ -e "${SOURCE_DIR}" ]]; then
        log_message "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 20
    fi
    export GIT_TERMINAL_PROMPT=0
    log_message "cloning protobuf v${SOFTWARE_VERSION} from ${PROTOBUF_SOURCE_URL}"
    if ! git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${PROTOBUF_SOURCE_URL}" "${SOURCE_DIR}"; then
        log_message "ERROR: failed to clone protobuf v${SOFTWARE_VERSION}"
        return 30
    fi
}

record_actual_version() {
    local version_output actual_version
    if ! version_output="$("${PROTOC_BIN}" --version 2>&1)"; then
        log_message "ERROR: built protoc cannot report its version"
        return 40
    fi
    actual_version="${version_output#libprotoc }"
    actual_version="${actual_version%%[[:space:]]*}"
    if [[ -z "${actual_version}" || "${actual_version}" == "${version_output}" ]]; then
        log_message "ERROR: unexpected protoc version output: ${version_output}"
        return 40
    fi
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log_message "ERROR: built protoc reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    fi
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
}

build_protobuf() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_build_commands || return $?
    if [[ -e "${SOURCE_DIR}" || -e "${BUILD_DIR}" || -e "${INSTALL_DIR}" ||
          -e "${BENCHMARK_REPO_DIR}" || -e "${GOOGLE_BENCHMARK_SOURCE_DIR}" ]]; then
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    prepare_benchmark_source || {
        log_message "ERROR: failed to prepare the pinned AccLibBenchmark source"
        return 40
    }
    clone_protobuf_source || return $?
    CXX_VERSION="$("${CXX_BINARY}" --version | head -n 1)"
    log_message "building protobuf with the upstream CMake Release configuration"
    if ! cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -Dprotobuf_BUILD_TESTS=OFF \
        -Dprotobuf_BUILD_SHARED_LIBS=OFF \
        -Dprotobuf_FORCE_FETCH_DEPENDENCIES=ON \
        -DCMAKE_INSTALL_PREFIX="${INSTALL_DIR}"; then
        log_message "ERROR: upstream CMake configuration failed"
        return 40
    fi
    if ! make -C "${BUILD_DIR}" -j"$(nproc)"; then
        log_message "ERROR: upstream CMake build failed"
        return 40
    fi
    if ! make -C "${BUILD_DIR}" install; then
        log_message "ERROR: private protobuf installation failed"
        return 40
    fi
    if [[ ! -x "${PROTOC_BIN}" ]]; then
        log_message "ERROR: private protoc was not installed: ${PROTOC_BIN}"
        return 40
    fi
    record_actual_version || return $?
    build_google_benchmark || {
        log_message "ERROR: failed to build Google Benchmark"
        return 40
    }
    log_message "building the original protobuf-benchmark C++ cases"
    if ! cmake -S "${SCRIPT_DIR}/scripts/benchmark_project" -B "${BENCHMARK_BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBENCHMARK_SOURCE_DIR="${BENCHMARK_SOURCE_DIR}" \
        -DCMAKE_PREFIX_PATH="${INSTALL_DIR};${GOOGLE_BENCHMARK_INSTALL_DIR}"; then
        log_message "ERROR: protobuf-benchmark CMake configuration failed"
        return 40
    fi
    if ! cmake --build "${BENCHMARK_BUILD_DIR}" --parallel "$(nproc)"; then
        log_message "ERROR: protobuf-benchmark C++ build failed"
        return 40
    fi
    [[ -x "${BENCHMARK_BIN}" && -x "${FIXTURE_DUMP_BIN}" ]] || return 40
    log_message "protobuf ${SOFTWARE_VERSION} and fixed-fixture C++ benchmark are ready"
}

start_protobuf_runtime() {
    initialize_runtime || return $?
    if [[ ! -x "${PROTOC_BIN}" ]]; then
        log_message "ERROR: private protoc is unavailable: ${PROTOC_BIN}"
        return 40
    fi
    if [[ ! -x "${BENCHMARK_BIN}" ]]; then
        log_message "ERROR: original protobuf-benchmark binary is unavailable"
        return 40
    fi
    if [[ ! -x "${FIXTURE_DUMP_BIN}" ]]; then
        log_message "ERROR: protobuf fixture generator is unavailable"
        return 40
    fi
    log_message "protobuf runtime is ready"
}

run_protobuf_benchmarks() {
    initialize_runtime || return $?
    if [[ ! -x "${PROTOC_BIN}" ]]; then
        log_message "ERROR: private protoc is unavailable: ${PROTOC_BIN}"
        return 40
    fi
    if [[ ! -x "${BENCHMARK_BIN}" ]]; then
        log_message "ERROR: original protobuf-benchmark binary is unavailable"
        return 40
    fi
    if [[ ! -x "${FIXTURE_DUMP_BIN}" ]]; then
        log_message "ERROR: protobuf fixture generator is unavailable"
        return 40
    fi
    local fixture_dir="${PERF_WORK_DIR}/fixtures"
    local repeat_dir="${PERF_WORK_DIR}/fixtures-repeat"
    mkdir -p "${fixture_dir}" "${repeat_dir}"
    "${FIXTURE_DUMP_BIN}" "${fixture_dir}" || return 50
    "${FIXTURE_DUMP_BIN}" "${repeat_dir}" || return 50
    (cd "${fixture_dir}" && LC_ALL=C sha256sum -- *.pb) \
        > "${RESULTS_DIR}/fixture_sha256.txt"
    (cd "${repeat_dir}" && LC_ALL=C sha256sum -- *.pb) \
        > "${RESULTS_DIR}/fixture_sha256_repeat.txt"
    if [[ "$(wc -l < "${RESULTS_DIR}/fixture_sha256.txt")" -ne 84 ]] ||
       ! cmp -s "${RESULTS_DIR}/fixture_sha256.txt" "${RESULTS_DIR}/fixture_sha256_repeat.txt"; then
        log_message "ERROR: expected 84 reproducible Protobuf fixtures"
        return 50
    fi
    rm -f -- "${RESULTS_DIR}/fixture_sha256_repeat.txt"
    log_message "verified 84 reproducible fixture hashes; compare fixture_sha256.txt across architectures"
    local cases_file="${RESULTS_DIR}/benchmark_cases.txt"
    if ! "${BENCHMARK_BIN}" --benchmark_list_tests=true > "${cases_file}"; then
        log_message "ERROR: could not list original protobuf-benchmark cases"
        return 50
    fi
    if [[ "$(wc -l < "${cases_file}")" -ne 168 ]]; then
        log_message "ERROR: expected 168 original protobuf-benchmark cases"
        return 50
    fi
    log_message "running all 168 original C++ cases (1s minimum, 5 repetitions)"
    if ! "${BENCHMARK_BIN}" --benchmark_min_time=1s \
        --benchmark_repetitions=5 --benchmark_display_aggregates_only=true \
        --benchmark_out="${RESULTS_DIR}/benchmark_google.json" \
        --benchmark_out_format=json \
        2>&1 | tee "${RESULTS_DIR}/benchmark_google.log"; then
        log_message "ERROR: original protobuf-benchmark execution failed"
        return 50
    fi
    if ! python3 "${SCRIPT_DIR}/scripts/parse_google_benchmark.py" \
        "${RESULTS_DIR}/benchmark_google.json" "${cases_file}" \
        "${RESULTS_DIR}/benchmark_protobuf.json"; then
        log_message "ERROR: failed to normalize Google Benchmark results"
        return 50
    fi
    log_message "original protobuf-benchmark results are ready"
}

stop_protobuf_runtime() {
    log_message "protobuf benchmark has no background service to stop"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 || "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        return 0
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/protobuf/local-* || "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/protobuf ]]; then
        log_message "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then stop_protobuf_runtime; fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then cleanup_standalone_workdir; fi
}

run_protobuf_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed" command_status="passed" finalize_status=0
    configure_runtime_paths || return $?
    trap emergency_standalone_cleanup EXIT
    initialize_runtime || return $?
    standalone_runtime system "${RESULTS_DIR}/system_info.json"
    standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"
    if build_protobuf; then
        standalone_runtime build-info "${RESULTS_DIR}/build_info.json" \
            "${SOFTWARE_VERSION}" "${PERF_ACTUAL_VERSION_FILE}" \
            "$(normalized_architecture "${EXPECTED_ARCH}")" "${PERF_RUN_ID}" \
            "${CXX_BINARY}" "${CXX_VERSION}" || { stage_status=$?; failed_stage="build"; }
    else
        stage_status=$?; failed_stage="build"
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_protobuf_runtime; then :; else stage_status=$?; failed_stage="start"; fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_protobuf_benchmarks; then :; else stage_status=$?; failed_stage="test"; fi
    fi
    if ! stop_protobuf_runtime; then cleanup_status="failed"; fi
    STANDALONE_STOP_DONE=1
    if ! standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"; then cleanup_status="failed"; fi
    if ! cleanup_standalone_workdir; then cleanup_status="failed"; fi
    STANDALONE_CLEANUP_DONE=1
    if [[ "${stage_status}" -ne 0 ]]; then command_status="failed"; fi
    if standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" \
        "$(normalized_architecture "${EXPECTED_ARCH}")" "${PERF_RUN_ID}" \
        "${command_status}" "${cleanup_status}" "${failed_stage}"; then
        :
    else
        finalize_status=$?
    fi
    trap - EXIT
    if [[ "${stage_status}" -ne 0 ]]; then return "${stage_status}"; fi
    if [[ "${cleanup_status}" != "passed" ]]; then return 70; fi
    return "${finalize_status}"
}

usage() {
    printf '%s\n' \
        "Usage: $(basename "$0") [--version VERSION] [--results-dir DIR] [--keep-workdir]" \
        "Build Protobuf and run the pinned AccLibBenchmark C++ serialization cases."
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version) SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir) RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) log_message "ERROR: unsupported option: $1"; return 10 ;;
        esac
    done
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}"
    : > "${RESULTS_DIR}/results.log"
    set +e
    run_protobuf_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    local pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
