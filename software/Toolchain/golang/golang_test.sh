#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.27.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

GO_RELEASE_URL="${GO_RELEASE_URL:-https://go.dev/dl}"
GO_OFFLINE_DIR="${GO_OFFLINE_DIR:-/home/runner/software/golang}"
GO_RUNTIME_ROOT="${GO_RUNTIME_ROOT:-}"

GO_INSTALL_DIR=""
GO_BIN=""
GO_ARCH=""
GO_RUNTIME_DIR=""
GCC_VERSION_STRING=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[golang] %s\n' "$*"
}

initialize_runtime() {
    local actual_arch expected_arch

    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64)
            expected_arch="x86_64"
            GO_ARCH="amd64"
            ;;
        aarch64|arm64)
            expected_arch="aarch64"
            GO_ARCH="arm64"
            ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) actual_arch="x86_64" ;;
        aarch64|arm64) actual_arch="aarch64" ;;
        *) actual_arch="$(uname -m)" ;;
    esac
    if [[ "${actual_arch}" != "${expected_arch}" ]]; then
        log "ERROR: expected architecture ${expected_arch}, runner is ${actual_arch}"
        return 20
    fi
    EXPECTED_ARCH="${expected_arch}"

    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/golang/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${GO_RUNTIME_ROOT}" ]]; then
        GO_RUNTIME_ROOT="${PERF_WORK_DIR}/runtime"
    fi

    GO_RUNTIME_DIR="${GO_RUNTIME_ROOT}"
    if ! mkdir -p "${GO_RUNTIME_DIR}/tmp"; then
        log "ERROR: cannot create the Go runtime directory: ${GO_RUNTIME_DIR}"
        return 30
    fi

    export TMPDIR="${GO_RUNTIME_DIR}/tmp"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
    GO_INSTALL_DIR="${PERF_WORK_DIR}/go-install"
    GO_BIN="${GO_INSTALL_DIR}/bin/go"
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

run_as_root() {
    if [[ "$1" == dnf && -n "${PERF_PROXY:-}" ]]; then
        set -- "$1" "--setopt=proxy=${PERF_PROXY}" "${@:2}"
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
        return
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: root privileges are required to install missing dependencies"
        return 30
    fi
    sudo -n "$@"
}

install_dependencies() {
    local missing=0 command_name
    for command_name in git curl gcc tar gzip sha256sum awk python3 perf nproc; do
        command -v "${command_name}" >/dev/null 2>&1 || missing=1
    done
    if [[ "${missing}" -eq 0 ]]; then
        return
    fi

    log "installing missing Go runtime and official benchmark dependencies"
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install Go dependencies"
        return 30
    fi
    run_as_root dnf install -y git curl gcc tar gzip coreutils gawk python3 perf || return 30

    for command_name in git curl gcc tar gzip sha256sum awk python3 perf nproc; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required command is still missing after installation: ${command_name}"
            return 30
        fi
    done
}

configure_go_environment() {
    export GOROOT="${GO_INSTALL_DIR}"
    # Bent invokes the Go command by its basename while it prepares each
    # official benchmark module.  Make this task's Go installation available
    # to those child processes instead of relying on a runner-wide Go command.
    export PATH="${GO_INSTALL_DIR}/bin:${PATH}"
    export GOCACHE="${GO_RUNTIME_DIR}/go-cache"
    export GOMODCACHE="${GO_RUNTIME_DIR}/go-mod-cache"
    export GOPATH="${GO_RUNTIME_DIR}/gopath"
    export GOTOOLCHAIN=local
    export GOENV=off
    export GOWORK=off
    if [[ "${PERF_DIRECT_NETWORK:-}" == "true" ]]; then
        export GOPROXY=direct
    else
        export GOPROXY="https://goproxy.cn"
    fi
    export GOSUMDB="${GOSUMDB:-sum.golang.org}"
    unset GOFLAGS GO111MODULE GOOS GOARCH GOARM GOAMD64
}

copy_or_download_go_archive() {
    local filename="$1" archive="$2" expected actual
    local offline_archive="${GO_OFFLINE_DIR}/${filename}"

    case "${SOFTWARE_VERSION}:${GO_ARCH}" in
        1.26.7:amd64)
            expected="ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca"
            ;;
        1.26.7:arm64)
            expected="5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37"
            ;;
        1.27.0:amd64)
            expected="675c26c449cbb18fc24b74650de1eabbae6e16f64326fd85a283fb3b58280685"
            ;;
        1.27.0:arm64)
            expected="51798d2c42d0e1c6ed7fd9f48728b4193abac9e8aad6dbac2fe96a81f5909bda"
            ;;
        *)
            log "ERROR: no verified Go binary release is declared for ${SOFTWARE_VERSION} on ${GO_ARCH}"
            return 10
            ;;
    esac

    if [[ -f "${offline_archive}" ]]; then
        log "using offline Go archive ${offline_archive}"
        cp "${offline_archive}" "${archive}"
    else
        log "downloading official Go archive ${filename}"
        if ! curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
            "${GO_RELEASE_URL}/${filename}"; then
            log "ERROR: failed to download ${filename}"
            return 30
        fi
    fi
    actual="$(sha256sum "${archive}" | awk '{print $1}')"
    if [[ "${actual}" != "${expected}" ]]; then
        log "ERROR: Go archive checksum mismatch: ${filename}"
        return 30
    fi
}

verify_golang_binary() {
    local actual_version actual_arch
    if [[ ! -x "${GO_BIN}" ]]; then
        log "ERROR: installed Go binary is missing: ${GO_BIN}"
        return 40
    fi
    actual_version="$("${GO_BIN}" version | awk '{print $3}' | sed 's/^go//')" || return 40
    actual_arch="$("${GO_BIN}" env GOARCH)" || return 40
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" || "${actual_arch}" != "${GO_ARCH}" ]]; then
        log "ERROR: installed Go is ${actual_version}/${actual_arch}, expected ${SOFTWARE_VERSION}/${GO_ARCH}"
        return 40
    fi
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
}

install_golang_binary() {
    local archive

    initialize_runtime || return $?
    install_dependencies || return $?
    if [[ -e "${GO_INSTALL_DIR}" ]]; then
        log "ERROR: installation directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    archive="${PERF_WORK_DIR}/go${SOFTWARE_VERSION}.linux-${GO_ARCH}.tar.gz"
    copy_or_download_go_archive "$(basename "${archive}")" "${archive}" || return $?
    if ! mkdir -p "${GO_INSTALL_DIR}" || \
       ! tar -xzf "${archive}" -C "${GO_INSTALL_DIR}" --strip-components=1; then
        log "ERROR: failed to install official Go binary ${SOFTWARE_VERSION}"
        return 30
    fi
    rm -f "${archive}"
    GCC_VERSION_STRING="$(gcc --version | head -n 1)"
    log "installed official precompiled Go ${SOFTWARE_VERSION} for ${GO_ARCH}"
    configure_go_environment
    verify_golang_binary || return $?
}

start_golang_runtime() {
    initialize_runtime || return $?
    configure_go_environment
    verify_golang_binary || return $?
    mkdir -p "${PERF_WORK_DIR}/smoke"
    printf 'package main\nfunc main() {}\n' > "${PERF_WORK_DIR}/smoke/main.go"
    if ! (
        cd "${PERF_WORK_DIR}/smoke"
        "${GO_BIN}" build -o smoke main.go
        ./smoke
    ); then
        log "ERROR: installed Go binary failed its smoke build"
        return 40
    fi
}

run_official_bent_benchmarks() {
    local bent_dir bent_module_dirs bent_module_dir bent_binary processor_count

    bent_dir="${GO_RUNTIME_DIR}/bent"
    rm -rf -- "${bent_dir}"
    mkdir -p "${bent_dir}" || return 50
    log "installing official Bent with go install"
    "${GO_BIN}" install golang.org/x/benchmarks/cmd/bent@latest || return 50
    bent_binary="${GOPATH}/bin/bent"
    if [[ ! -x "${bent_binary}" ]]; then
        log "ERROR: go install did not create Bent: ${bent_binary}"
        return 50
    fi
    shopt -s nullglob
    bent_module_dirs=("${GOMODCACHE}"/golang.org/x/benchmarks@*/cmd/bent)
    shopt -u nullglob
    if [[ "${#bent_module_dirs[@]}" -ne 1 ]]; then
        log "ERROR: expected one installed Bent module directory, found ${#bent_module_dirs[@]}"
        return 50
    fi
    bent_module_dir="${bent_module_dirs[0]}"
    # Bent prepares its own scripts and Dockerfile with -I.  The test itself
    # remains the official command requested for this case: bent -N 15.
    if ! (
        cd "${bent_dir}"
        "${bent_binary}" -I
    ); then
        log "ERROR: Bent initialization failed"
        return 50
    fi
    if ! cp "${bent_module_dir}/configs/suites.toml" "${bent_dir}/suites.toml" || \
       ! cp "${bent_module_dir}/configs/benchmarks-100.toml" "${bent_dir}/benchmarks-100.toml"; then
        log "ERROR: Bent module is missing its official suite or benchmarks-100 configuration"
        return 50
    fi
    processor_count="$(nproc)"
    if [[ ! "${processor_count}" =~ ^[1-9][0-9]*$ ]]; then
        log "ERROR: nproc returned an invalid processor count: ${processor_count}"
        return 50
    fi
    if ! printf '[[Configurations]]\n  Name = "Baseline"\n  RunEnv = ["GOGC=100", "GOMAXPROCS=%s"]\n  Root = "%s"\n' \
        "${processor_count}" "${GO_INSTALL_DIR}" > "${bent_dir}/configurations.toml"; then
        log "ERROR: cannot write Bent Baseline configuration"
        return 50
    fi
    log "running official Bent benchmark suite: bent -N 15"
    (
        cd "${bent_dir}"
        "${bent_binary}" -N 15
    )
}

run_golang_benchmarks() {
    initialize_runtime || return $?
    configure_go_environment
    verify_golang_binary || return $?
    log "running official Bent benchmark suite"
    if ! (
        run_official_bent_benchmarks
    ) 2>&1 | tee "${RESULTS_DIR}/benchmark_go_bench.txt"; then
        log "ERROR: official Bent benchmark failed"
        return 50
    fi
    if [[ ! -s "${RESULTS_DIR}/benchmark_go_bench.txt" ]]; then
        log "ERROR: official Go benchmark output is empty"
        return 50
    fi
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/benchmark_go_bench.txt" \
        "${RESULTS_DIR}/benchmark_golang.json" || return 50
}

stop_golang_runtime() {
    initialize_runtime || return $?
    configure_go_environment
    if [[ ! -e "${GO_RUNTIME_DIR}" ]]; then
        log "Go benchmark suite has no background service to stop"
        return
    fi
    if [[ "${GO_RUNTIME_DIR}" != "${GO_RUNTIME_ROOT}" ]]; then
        log "ERROR: refusing to clean unexpected Go runtime directory: ${GO_RUNTIME_DIR}"
        return 50
    fi
    if ! chmod -R u+w "${GO_RUNTIME_DIR}"; then
        log "ERROR: failed to make the private Go runtime directory writable for cleanup"
        return 50
    fi
    if ! rm -rf -- "${GO_RUNTIME_DIR}"; then
        log "ERROR: failed to remove the private Go runtime directory"
        return 50
    fi
    log "removed private Go runtime directory: ${GO_RUNTIME_DIR}"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping standalone work directory: ${PERF_WORK_DIR}"
        return
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log "external work directory was not removed: ${PERF_WORK_DIR}"
        return
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/golang/local-* || "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/golang ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}"
}

run_golang_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed"
    local command_status="passed" finalize_status=0

    initialize_runtime || return $?
    trap 'stop_golang_runtime || :; cleanup_standalone_workdir || :' EXIT

    if standalone_runtime system "${RESULTS_DIR}/system_info.json"; then
        :
    else
        stage_status=$?
        failed_stage="prepare"
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
            :
        else
            stage_status=$?
            failed_stage="prepare"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if install_golang_binary; then
            :
        else
            stage_status=$?
            failed_stage="build"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if standalone_runtime build-info "${RESULTS_DIR}/build_info.json" \
            "${SOFTWARE_VERSION}" "${PERF_ACTUAL_VERSION_FILE}" "${EXPECTED_ARCH}" \
            "${PERF_RUN_ID}" "${GCC_VERSION_STRING}" \
            --release-url="${GO_RELEASE_URL}"; then
            :
        else
            stage_status=$?
            failed_stage="build"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_golang_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_golang_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_golang_runtime; then
        cleanup_status="failed"
    fi
    if ! standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"; then
        cleanup_status="failed"
    fi
    if ! cleanup_standalone_workdir; then
        cleanup_status="failed"
    fi
    trap - EXIT

    if [[ "${stage_status}" -ne 0 ]]; then
        command_status="failed"
    fi
    if standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
        "${PERF_RUN_ID}" "${command_status}" "${cleanup_status}" "${failed_stage}"; then
        finalize_status=0
    else
        finalize_status=$?
    fi

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

Install the official precompiled Go release and run the official
golang.org/x/benchmarks/cmd/bench suite.

Options:
  --version VERSION       Go version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Offline archives take priority from ${GO_OFFLINE_DIR}:
  go<VERSION>.linux-<amd64|arm64>.tar.gz
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
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) log "ERROR: unsupported option: $1"; return 10 ;;
        esac
    done
    initialize_runtime || return $?
    : > "${RESULTS_DIR}/results.log"
    set +e
    run_golang_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    local result="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${result}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
