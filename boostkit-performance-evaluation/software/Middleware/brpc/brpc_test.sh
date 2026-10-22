#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.17.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
BRPC_SOURCE_URL="${BRPC_SOURCE_URL:-https://github.com/apache/brpc.git}"
RDMA_SERVER_PORT=8003
RDMA_CLIENT_DUMMY_PORT=8001
BRPC_ATTACHMENT_SIZES=(0 1024 4096 8192 102400 204800 1048576 8388608)
BRPC_REPETITIONS=5
BRPC_MAX_INFLIGHT_ATTACHMENT_BYTES=$((32 * 1024 * 1024))
BRPC_MAX_CLIENT_THREADS=32
BRPC_MAX_QUEUE_DEPTH=32

SOURCE_DIR=""
BUILD_DIR=""
EXAMPLE_DIR=""
SERVICE_DIR=""
RDMA_SERVER_BIN=""
RDMA_CLIENT_BIN=""
SERVER_LOG_FILE=""
CLIENT_LOG_FILE=""
SERVER_PID_FILE=""
COMPILER_BINARY=""
COMPILER_VERSION_STRING=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log_message() {
    printf '[brpc] %s\n' "$*"
}

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log_message "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    }
    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64) EXPECTED_ARCH="x86_64" ;;
        aarch64|arm64) EXPECTED_ARCH="aarch64" ;;
        *) EXPECTED_ARCH="${EXPECTED_ARCH,,}" ;;
    esac
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/brpc/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/brpc-source"
    # The upstream example discovers */output/include inside the source tree.
    BUILD_DIR="${SOURCE_DIR}/build"
    EXAMPLE_DIR="${SOURCE_DIR}/example/rdma_performance"
    SERVICE_DIR="${PERF_WORK_DIR}/service"
    RDMA_SERVER_BIN="${EXAMPLE_DIR}/build/server"
    RDMA_CLIENT_BIN="${EXAMPLE_DIR}/build/client"
    SERVER_LOG_FILE="${SERVICE_DIR}/server.log"
    CLIENT_LOG_FILE="${RESULTS_DIR}/benchmark_client.log"
    SERVER_PID_FILE="${SERVICE_DIR}/server.pid"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    local runner_architecture

    configure_runtime_paths
    runner_architecture="$(uname -m)"
    [[ "${runner_architecture}" == "${EXPECTED_ARCH}" ]] || {
        log_message "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    }
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_brpc_dependencies() {
    local required package library header
    local packages=()

    for required in git cmake make gcc g++ protoc python3 curl sed grep install tee nproc; do
        if command -v "${required}" >/dev/null 2>&1; then
            continue
        fi
        case "${required}" in
            git) package="git" ;;
            cmake) package="cmake" ;;
            make) package="make" ;;
            gcc) package="gcc" ;;
            g++) package="gcc-c++" ;;
            protoc) package="protobuf-compiler" ;;
            python3) package="python3" ;;
            curl) package="curl" ;;
            sed) package="sed" ;;
            grep) package="grep" ;;
            install|tee|nproc) package="coreutils" ;;
        esac
        log_message "missing required BRPC build command: ${required}"
        packages+=("${package}")
    done

    # BRPC links against these system libraries.  Test the headers instead of
    # assuming that a package name proves the compiler can use the library.
    for library in openssl gflags leveldb protobuf rdma; do
        case "${library}" in
            openssl) header="openssl/ssl.h"; package="openssl-devel" ;;
            gflags) header="gflags/gflags.h"; package="gflags-devel" ;;
            leveldb) header="leveldb/db.h"; package="leveldb-devel" ;;
            protobuf) header="google/protobuf/message.h"; package="protobuf-devel" ;;
            rdma) header="infiniband/verbs.h"; package="rdma-core-devel" ;;
        esac
        if ! printf '#include <%s>\nint main(){return 0;}\n' "${header}" \
            | g++ -x c++ -fsyntax-only - 2>/dev/null; then
            log_message "missing required BRPC development headers: ${library}"
            packages+=("${package}")
        fi
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log_message "ERROR: dnf is required to install BRPC build prerequisites"
        return 30
    fi
    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log_message "installing missing BRPC build packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log_message "ERROR: sudo is required to install BRPC build prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log_message "ERROR: failed to install BRPC build prerequisites"
        return 30
    fi

    for required in git cmake make gcc g++ protoc python3 curl sed grep install tee nproc; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log_message "ERROR: required BRPC build command remains unavailable: ${required}"
            return 30
        fi
    done
    for library in openssl gflags leveldb protobuf rdma; do
        case "${library}" in
            openssl) header="openssl/ssl.h" ;;
            gflags) header="gflags/gflags.h" ;;
            leveldb) header="leveldb/db.h" ;;
            protobuf) header="google/protobuf/message.h" ;;
            rdma) header="infiniband/verbs.h" ;;
        esac
        if ! printf '#include <%s>\nint main(){return 0;}\n' "${header}" \
            | g++ -x c++ -fsyntax-only - 2>/dev/null; then
            log_message "ERROR: required BRPC development headers remain unavailable: ${library}"
            return 30
        fi
    done
}

configure_rdma_example_for_modern_protobuf() {
    # The example CMake is configured separately from the upstream library.
    # Only its private build file is adjusted; server.cpp/client.cpp stay intact.
    local example_cmake_file="${EXAMPLE_DIR}/CMakeLists.txt"
    local absl_cmake_file="${EXAMPLE_DIR}/brpc_rdma_example_absl.cmake"
    [[ -f "${example_cmake_file}" ]] || {
        log_message "ERROR: official RDMA performance example CMake file is missing: ${example_cmake_file}"
        return 40
    }
    sed -i 's/set(CMAKE_CXX_STANDARD 11)/set(CMAKE_CXX_STANDARD 17)/' \
        "${example_cmake_file}" || {
        log_message "ERROR: could not enable C++17 for the official performance example"
        return 40
    }
    grep -Fq 'set(CMAKE_CXX_STANDARD 17)' "${example_cmake_file}" || {
        log_message "ERROR: the official example C++11 setting was not found"
        return 40
    }

    install -m 0644 "${SCRIPT_DIR}/rdma_example_absl.cmake" "${absl_cmake_file}" || {
        log_message "ERROR: could not provide the Abseil dependencies for the official example"
        return 40
    }
    sed -i \
        -e '/^include(FindProtobuf)$/a\include(${CMAKE_CURRENT_LIST_DIR}/brpc_rdma_example_absl.cmake)' \
        -e '/^[[:space:]]*${PROTOBUF_LIBRARIES}[[:space:]]*$/a\    ${BRPC_RDMA_EXAMPLE_ABSL_TARGETS}' \
        -e '/^[[:space:]]*${THRIFT_LIB}[[:space:]]*$/a\    ${RDMA_LIB}\n    z' \
        "${example_cmake_file}" || {
        log_message "ERROR: could not link the official example dependencies"
        return 40
    }
    grep -Fq 'include(${CMAKE_CURRENT_LIST_DIR}/brpc_rdma_example_absl.cmake)' "${example_cmake_file}" \
        && grep -Fq '${BRPC_RDMA_EXAMPLE_ABSL_TARGETS}' "${example_cmake_file}" \
        && grep -Fq '    ${RDMA_LIB}' "${example_cmake_file}" || {
        log_message "ERROR: official example dependency configuration is incomplete"
        return 40
    }
    log_message "configured the official performance example for current Protobuf and Abseil"
}

build_brpc() {
    local actual_version

    initialize_runtime || return $?
    require_brpc_dependencies || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" && ! -e "${SERVICE_DIR}" ]] || {
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }

    # The official getting_started.md builds BRPC with the default g++
    # toolchain.  Record the compiler actually used by this build.
    COMPILER_BINARY="g++"
    COMPILER_VERSION_STRING="$("${COMPILER_BINARY}" --version | head -n 1)"
    log_message "using compiler: ${COMPILER_BINARY} (${COMPILER_VERSION_STRING})"

    export GIT_TERMINAL_PROMPT=0
    log_message "cloning brpc ${SOFTWARE_VERSION} from ${BRPC_SOURCE_URL}"
    git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${BRPC_SOURCE_URL}" "${SOURCE_DIR}" || {
        log_message "ERROR: failed to clone brpc ${SOFTWARE_VERSION}"
        return 30
    }

    # The server binary has no --version switch.  RELEASE_VERSION from the
    # checked-out release is the authoritative version evidence.
    actual_version="$(<"${SOURCE_DIR}/RELEASE_VERSION")"
    actual_version="${actual_version//[[:space:]]/}"
    [[ "${actual_version}" == "${SOFTWARE_VERSION}" ]] || {
        log_message "ERROR: source RELEASE_VERSION '${actual_version}' does not match ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40

    configure_rdma_example_for_modern_protobuf || return $?

    log_message "building the official brpc library with cmake (Release)"
    mkdir -p "${BUILD_DIR}"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DWITH_DEBUG_SYMBOLS=OFF \
        -DWITH_RDMA=ON \
        -DBUILD_BRPC_TOOLS=OFF || {
        log_message "ERROR: cmake configure of the brpc library failed"
        return 40
    }
    cmake --build "${BUILD_DIR}" -j "$(nproc)" || {
        log_message "ERROR: cmake build of the brpc library failed"
        return 40
    }
    [[ -f "${BUILD_DIR}/output/lib/libbrpc.a" ]] || {
        log_message "ERROR: official libbrpc.a was not produced under ${BUILD_DIR}/output/lib"
        return 40
    }

    # RDMA support is a compile-time requirement of this upstream example;
    # --use_rdma=false on both processes keeps the measured transport TCP.
    log_message "building official example/rdma_performance server and client"
    mkdir -p "${EXAMPLE_DIR}/build"
    cmake -S "${EXAMPLE_DIR}" -B "${EXAMPLE_DIR}/build" \
        -DCMAKE_BUILD_TYPE=Release || {
        log_message "ERROR: cmake configure of example/rdma_performance failed"
        return 40
    }
    cmake --build "${EXAMPLE_DIR}/build" -j "$(nproc)" \
        --target client --target server || {
        log_message "ERROR: cmake build of rdma_performance client/server failed"
        return 40
    }
    [[ -x "${RDMA_SERVER_BIN}" ]] || {
        log_message "ERROR: official server binary was not produced: ${RDMA_SERVER_BIN}"
        return 40
    }
    [[ -x "${RDMA_CLIENT_BIN}" ]] || {
        log_message "ERROR: official client binary was not produced: ${RDMA_CLIENT_BIN}"
        return 40
    }
    log_message "brpc ${SOFTWARE_VERSION} official TCP performance example is ready"
}

port_is_free() {
    python3 - "$1" <<'PYEOF'
import socket
import sys

port = int(sys.argv[1])
with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError:
        sys.exit(1)
PYEOF
}

process_is_alive() {
    [[ -n "$1" && "$1" =~ ^[0-9]+$ ]] && kill -0 "$1" 2>/dev/null
}

read_pid_file() {
    local pid_file="$1"
    [[ -f "${pid_file}" ]] || return 0
    local pid
    pid="$(<"${pid_file}")"
    pid="${pid//[[:space:]]/}"
    [[ "${pid}" =~ ^[0-9]+$ ]] && printf '%s\n' "${pid}"
}

terminate_process_gracefully() {
    local pid="$1" name="$2" waited=0
    process_is_alive "${pid}" || return 0
    kill -TERM "${pid}" 2>/dev/null || true
    while process_is_alive "${pid}" && ((waited < 15)); do
        sleep 1
        waited=$((waited + 1))
    done
    if process_is_alive "${pid}"; then
        kill -KILL "${pid}" 2>/dev/null || true
        sleep 1
    fi
    if process_is_alive "${pid}"; then
        log_message "ERROR: ${name} (pid ${pid}) is still alive"
        return 1
    fi
    return 0
}

start_brpc_service() {
    local server_pid attempt

    initialize_runtime || return $?
    [[ -x "${RDMA_SERVER_BIN}" ]] || {
        log_message "ERROR: official performance server is unavailable: ${RDMA_SERVER_BIN}"
        return 40
    }
    [[ -e "${SERVICE_DIR}" ]] || mkdir -p "${SERVICE_DIR}"
    if process_is_alive "$(read_pid_file "${SERVER_PID_FILE}")"; then
        log_message "official performance server is already running"
        return 0
    fi
    port_is_free "${RDMA_SERVER_PORT}" || {
        log_message "ERROR: port ${RDMA_SERVER_PORT} is already in use"
        return 20
    }
    log_message "starting original performance server on port ${RDMA_SERVER_PORT} (TCP)"
    nohup "${RDMA_SERVER_BIN}" \
        --use_rdma=false \
        --port="${RDMA_SERVER_PORT}" \
        --bthread_concurrency=32 \
        >"${SERVER_LOG_FILE}" 2>&1 &
    server_pid=$!
    printf '%s\n' "${server_pid}" > "${SERVER_PID_FILE}"
    for ((attempt = 0; attempt < 60; attempt += 1)); do
        if ! process_is_alive "${server_pid}"; then
            break
        fi
        if curl --noproxy '*' -fsS --max-time 1 -o /dev/null \
            "http://127.0.0.1:${RDMA_SERVER_PORT}/status" 2>/dev/null; then
            break
        fi
        sleep 1
    done
    if ! process_is_alive "${server_pid}" ||
       ! curl --noproxy '*' -fsS --max-time 2 -o /dev/null \
           "http://127.0.0.1:${RDMA_SERVER_PORT}/status"; then
        terminate_process_gracefully "${server_pid}" "official performance server" || true
        log_message "ERROR: official performance server did not become ready; see ${SERVER_LOG_FILE}"
        return 40
    fi
    log_message "official performance server is ready (pid ${server_pid})"
}

run_brpc_rdma_performance() {
    local server_pid attachment_size repetition client_threads queue_depth max_inflight

    initialize_runtime || return $?
    [[ -x "${RDMA_CLIENT_BIN}" ]] || {
        log_message "ERROR: official performance client is unavailable: ${RDMA_CLIENT_BIN}"
        return 40
    }
    server_pid="$(read_pid_file "${SERVER_PID_FILE}")"
    process_is_alive "${server_pid}" || {
        log_message "ERROR: official performance server (pid ${server_pid:-unknown}) is not running"
        return 40
    }
    curl --noproxy '*' -fsS --max-time 2 -o /dev/null \
        "http://127.0.0.1:${RDMA_SERVER_PORT}/status" || {
        log_message "ERROR: official performance server is not responding"
        return 40
    }
    port_is_free "${RDMA_CLIENT_DUMMY_PORT}" || {
        log_message "ERROR: client dummy port ${RDMA_CLIENT_DUMMY_PORT} is already in use"
        return 20
    }
    : > "${CLIENT_LOG_FILE}"
    for attachment_size in "${BRPC_ATTACHMENT_SIZES[@]}"; do
        client_threads="${BRPC_MAX_CLIENT_THREADS}"
        queue_depth="${BRPC_MAX_QUEUE_DEPTH}"
        if ((attachment_size > 0)); then
            max_inflight=$((BRPC_MAX_INFLIGHT_ATTACHMENT_BYTES / attachment_size))
            if ((max_inflight < client_threads)); then
                client_threads="${max_inflight}"
            fi
            queue_depth=$((max_inflight / client_threads))
            if ((queue_depth > BRPC_MAX_QUEUE_DEPTH)); then
                queue_depth="${BRPC_MAX_QUEUE_DEPTH}"
            fi
        fi
        for ((repetition = 1; repetition <= BRPC_REPETITIONS; repetition += 1)); do
            log_message "running TCP baidu_std echo: ${attachment_size}B, ${client_threads} threads, depth ${queue_depth}, repetition ${repetition}/${BRPC_REPETITIONS} (20s)"
            if ! "${RDMA_CLIENT_BIN}" \
                --use_rdma=false \
                --protocol=baidu_std \
                --connection_type=single \
                --servers="127.0.0.1:${RDMA_SERVER_PORT}" \
                --thread_num="${client_threads}" \
                --queue_depth="${queue_depth}" \
                --bthread_concurrency=160 \
                --attachment_size="${attachment_size}" \
                --echo_attachment=true \
                --test_seconds=20 2>&1 | tee -a "${CLIENT_LOG_FILE}"; then
                log_message "ERROR: original performance client failed for ${attachment_size}B, repetition ${repetition}"
                return 50
            fi
        done
    done
    export SOFTWARE_VERSION EXPECTED_ARCH
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${CLIENT_LOG_FILE}" \
        "${RESULTS_DIR}/benchmark_brpc.json" || {
        log_message "ERROR: failed to normalize official client results"
        return 50
    }
    log_message "brpc benchmark results written to benchmark_client.log and benchmark_brpc.json"
}

stop_brpc_service() {
    configure_runtime_paths || return $?
    local stop_status=0 server_pid
    server_pid="$(read_pid_file "${SERVER_PID_FILE}")"
    if process_is_alive "${server_pid}"; then
        terminate_process_gracefully "${server_pid}" "official performance server" || stop_status=1
    fi
    rm -f "${SERVER_PID_FILE}"
    if [[ -n "${server_pid}" ]] && port_is_free "${RDMA_SERVER_PORT}"; then
        log_message "official performance server has stopped"
    elif [[ -n "${server_pid}" ]]; then
        log_message "ERROR: port ${RDMA_SERVER_PORT} is still occupied after stop"
        stop_status=1
    else
        log_message "brpc benchmark has no running service to stop"
    fi
    return "${stop_status}"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/brpc/local-* && \
       "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/brpc" ]] || {
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
        stop_brpc_service
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_brpc_standalone() {
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
        if build_brpc; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
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
        if start_brpc_service; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_brpc_rdma_performance; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_brpc_service; then
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
    [[ "${stage_status}" -eq 0 ]] || return "${stage_status}"
    [[ "${cleanup_status}" == "passed" ]] || return 70
    return "${finalize_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Build and run brpc's official example/rdma_performance client/server over TCP.
Results
default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       brpc version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, BRPC_SOURCE_URL,
  PERF_PROXY
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
    run_brpc_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
