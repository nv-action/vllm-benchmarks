#!/usr/bin/env bash
# Kubernetes performance case (official source benchmarks).
#
# The software under test is the upstream Kubernetes repository
# (https://github.com/kubernetes/kubernetes) cloned at an exact release tag.
# The repository is fully vendored, so its own official `go test -bench`
# benchmarks are compiled and executed hermetically (go.work workspace mode with
# vendor mode, network disabled) inside an isolated work directory.
#
# Go itself is NOT installed from the system package manager: a task-private
# official Go toolchain (verified sha256) is unpacked from the runner offline
# golang directory into ${PERF_WORK_DIR}/go-install and used via GOROOT/PATH.
#
# Benchmarked packages (pure-compute, no etcd/container runtime required):
#   pkg/apis/core/v1                                - core API list conversion
#   pkg/registry/core/service/ipallocator           - service ClusterIP allocation
#   pkg/controller/nodeipam/ipam/cidrset            - node CIDR set allocation
#
# The four framework stages map to: clone+compile the pinned release (build),
# smoke-run every benchmark binary (start), run the full benchmark suite and
# aggregate metrics (test), and remove the work directory (stop).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_NAME="kubernetes"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.37.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

KUBERNETES_REPOSITORY="${KUBERNETES_REPOSITORY:-https://github.com/kubernetes/kubernetes}"
KUBERNETES_BENCHTIME="${KUBERNETES_BENCHTIME:-1s}"
GO_VERSION="${GO_VERSION:-1.26.7}"
GO_RELEASE_URL="${GO_RELEASE_URL:-https://go.dev/dl}"
GO_OFFLINE_DIR="${GO_OFFLINE_DIR:-/home/runner/software/golang}"

# package:group:binary-name triples for the benchmark suite.
BENCH_PACKAGES=(
    "pkg/apis/core/v1:api_conversion:bench_apis_core_v1"
    "pkg/registry/core/service/ipallocator:ip_allocator:bench_ipallocator"
    "pkg/controller/nodeipam/ipam/cidrset:cidr_set:bench_cidrset"
)

# Lifecycle paths (assigned in configure_runtime_paths).
SOURCE_DIR=""
BENCH_BIN_DIR=""
SMOKE_LOG=""
RAW_LOG=""
GO_INSTALL_DIR=""
GO_BIN=""
GO_ARCH=""

log() {
    printf '[%s] %s\n' "${SOFTWARE_NAME}" "$*"
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/kubernetes/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    case "${EXPECTED_ARCH}" in
        x86_64) GO_ARCH="amd64" ;;
        aarch64) GO_ARCH="arm64" ;;
    esac
    GO_INSTALL_DIR="${PERF_WORK_DIR}/go-install"
    GO_BIN="${GO_INSTALL_DIR}/bin/go"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    SOURCE_DIR="${PERF_WORK_DIR}/kubernetes"
    BENCH_BIN_DIR="${PERF_WORK_DIR}/bin"
    SMOKE_LOG="${RESULTS_DIR}/smoke_kubernetes.log"
    RAW_LOG="${RESULTS_DIR}/benchmark_kubernetes_raw.log"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_kubernetes_tools() {
    local command_name package
    local packages=()

    for command_name in git python3 tee sha256sum awk; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            git) package="git" ;;
            python3) package="python3" ;;
            tee|sha256sum) package="coreutils" ;;
            awk) package="gawk" ;;
        esac
        log "missing required ${SOFTWARE_NAME} test command: ${command_name}"
        packages+=("${package}")
    done

    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install ${SOFTWARE_NAME} test prerequisites"
        return 30
    fi

    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing ${SOFTWARE_NAME} test packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install ${SOFTWARE_NAME} test prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install ${SOFTWARE_NAME} test prerequisites"
        return 30
    fi

    for command_name in git python3 tee sha256sum awk; do
        if ! command -v "${command_name}" >/dev/null 2>&1; then
            log "ERROR: required ${SOFTWARE_NAME} test command remains unavailable: ${command_name}"
            return 30
        fi
    done
}

configure_go_environment() {
    export GOROOT="${GO_INSTALL_DIR}"
    export PATH="${GO_INSTALL_DIR}/bin:${PATH}"
    export GOCACHE="${PERF_WORK_DIR}/go-cache"
    export GOTOOLCHAIN=local
    export GOENV=off
}

copy_or_download_go_archive() {
    local filename="$1" archive="$2" expected actual
    local offline_archive="${GO_OFFLINE_DIR}/${filename}"

    case "${GO_VERSION}:${GO_ARCH}" in
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
            log "ERROR: no verified Go binary release is declared for ${GO_VERSION} on ${GO_ARCH}"
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

verify_go_binary() {
    local actual_version actual_arch
    if [[ ! -x "${GO_BIN}" ]]; then
        log "ERROR: private Go binary is unavailable: ${GO_BIN}"
        return 40
    fi
    actual_version="$("${GO_BIN}" version | awk '{print $3}' | sed 's/^go//')" || return 40
    actual_arch="$("${GO_BIN}" env GOARCH)" || return 40
    if [[ "${actual_version}" != "${GO_VERSION}" || "${actual_arch}" != "${GO_ARCH}" ]]; then
        log "ERROR: private Go is ${actual_version}/${actual_arch}, expected ${GO_VERSION}/${GO_ARCH}"
        return 40
    fi
}

install_go_toolchain() {
    local archive

    if [[ -e "${GO_INSTALL_DIR}" ]]; then
        log "ERROR: Go toolchain directory is not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    archive="${PERF_WORK_DIR}/go${GO_VERSION}.linux-${GO_ARCH}.tar.gz"
    copy_or_download_go_archive "$(basename "${archive}")" "${archive}" || return $?
    if ! mkdir -p "${GO_INSTALL_DIR}" || \
       ! tar -xzf "${archive}" -C "${GO_INSTALL_DIR}" --strip-components=1; then
        log "ERROR: failed to install official Go ${GO_VERSION} for ${GO_ARCH}"
        return 30
    fi
    rm -f "${archive}"
    configure_go_environment
    verify_go_binary || return $?
    log "installed task-private Go ${GO_VERSION} for ${GO_ARCH} from the golang offline directory"
}

build_kubernetes() {
    local runner_architecture
    local tag
    local entry
    local package group bin_name

    initialize_runtime || return $?
    if [[ ! "${SOFTWARE_VERSION}" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        log "ERROR: invalid kubernetes release version: ${SOFTWARE_VERSION}"
        return 20
    fi
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    require_kubernetes_tools || return $?
    if [[ -e "${SOURCE_DIR}" || -e "${GO_INSTALL_DIR}" ]]; then
        log "ERROR: build output is not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    install_go_toolchain || return $?

    log "cloning kubernetes v${SOFTWARE_VERSION} from ${KUBERNETES_REPOSITORY}"
    git clone --depth 1 --branch "v${SOFTWARE_VERSION}" \
        "${KUBERNETES_REPOSITORY}" "${SOURCE_DIR}" || {
        log "ERROR: failed to clone ${KUBERNETES_REPOSITORY} at v${SOFTWARE_VERSION}"
        return 40
    }
    tag="$(git -C "${SOURCE_DIR}" describe --tags --exact-match HEAD 2>/dev/null || true)"
    if [[ "${tag}" != "v${SOFTWARE_VERSION}" ]]; then
        log "ERROR: cloned source is at ${tag:-unknown}, expected v${SOFTWARE_VERSION}"
        return 40
    fi

    mkdir -p "${BENCH_BIN_DIR}"
    for entry in "${BENCH_PACKAGES[@]}"; do
        IFS=':' read -r package group bin_name <<<"${entry}"
        log "compiling benchmark binary for ${package}"
        (
            cd "${SOURCE_DIR}"
            env GOFLAGS=-mod=vendor GOPROXY=off GOSUMDB=off \
                GOTOOLCHAIN=local \
                go test -c -o "${BENCH_BIN_DIR}/${bin_name}.test" "./${package}"
        ) || {
            log "ERROR: failed to compile the benchmark binary for ${package}"
            return 40
        }
    done

    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "kubernetes v${SOFTWARE_VERSION} deployed with vendored dependencies"
}

start_kubernetes_smoke() {
    local entry
    local package group bin_name
    local bench_lines

    initialize_runtime || return $?
    if [[ ! -d "${BENCH_BIN_DIR}" ]]; then
        log "ERROR: ${SOFTWARE_NAME} is not deployed (run build first)"
        return 40
    fi
    : > "${SMOKE_LOG}"
    for entry in "${BENCH_PACKAGES[@]}"; do
        IFS=':' read -r package group bin_name <<<"${entry}"
        if [[ ! -x "${BENCH_BIN_DIR}/${bin_name}.test" ]]; then
            log "ERROR: benchmark binary for ${package} is missing"
            return 40
        fi
        log "smoke-running the ${package} benchmark binary"
        if ! "${BENCH_BIN_DIR}/${bin_name}.test" \
                -test.run='^$' -test.bench=. -test.benchtime=1x \
                -test.timeout=10m >>"${SMOKE_LOG}" 2>&1; then
            log "ERROR: the ${package} benchmark smoke run failed (see ${SMOKE_LOG})"
            return 40
        fi
        bench_lines="$(grep -c '^Benchmark' "${SMOKE_LOG}" || true)"
        if [[ "${bench_lines}" -lt 1 ]]; then
            log "ERROR: the ${package} smoke run produced no benchmark output"
            return 40
        fi
    done
    log "all kubernetes benchmark binaries are ready"
}

run_kubernetes_benchmarks() {
    local entry
    local package group bin_name
    local pipeline_status=0

    initialize_runtime || return $?
    if [[ ! -d "${BENCH_BIN_DIR}" ]]; then
        log "ERROR: ${SOFTWARE_NAME} is not deployed (run build first)"
        return 50
    fi
    rm -f "${RAW_LOG}"

    for entry in "${BENCH_PACKAGES[@]}"; do
        IFS=':' read -r package group bin_name <<<"${entry}"
        if [[ ! -x "${BENCH_BIN_DIR}/${bin_name}.test" ]]; then
            log "ERROR: benchmark binary for ${package} is missing"
            return 50
        fi
        log "running the ${package} benchmarks (benchtime ${KUBERNETES_BENCHTIME})"
        printf '### PACKAGE %s %s\n' "${package}" "${group}" | tee -a "${RAW_LOG}"
        set +e
        "${BENCH_BIN_DIR}/${bin_name}.test" \
            -test.run='^$' -test.bench=. -test.benchtime="${KUBERNETES_BENCHTIME}" \
            -test.count=1 -test.timeout=20m 2>&1 | tee -a "${RAW_LOG}"
        pipeline_status="${PIPESTATUS[0]}"
        set -e
        if [[ "${pipeline_status}" -ne 0 ]]; then
            log "ERROR: the ${package} benchmark run failed (see ${RAW_LOG})"
            return 50
        fi
    done

    set +e
    env SOFTWARE_VERSION="${SOFTWARE_VERSION}" \
        EXPECTED_ARCH="${EXPECTED_ARCH}" \
        PERF_RUN_ID="${PERF_RUN_ID}" \
        GO_VERSION="$("${GO_BIN}" env GOVERSION 2>/dev/null || echo unknown)" \
        KUBERNETES_REPOSITORY="${KUBERNETES_REPOSITORY}" \
        python3 "${SCRIPT_DIR}/scripts/aggregate_results.py" \
            --raw-log "${RAW_LOG}" \
            --results-dir "${RESULTS_DIR}" \
            --output "${RESULTS_DIR}/results.json" \
            --benchtime "${KUBERNETES_BENCHTIME}" 2>&1 | tee -a "${RAW_LOG}"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    if [[ "${pipeline_status}" -ne 0 ]]; then
        log "ERROR: kubernetes benchmark aggregation failed (see ${RAW_LOG})"
        return 50
    fi
    if [[ ! -s "${RESULTS_DIR}/results.json" ]]; then
        log "ERROR: the benchmark suite produced no aggregate results"
        return 50
    fi
    log "kubernetes benchmark suite completed"
}

stop_kubernetes_runtime() {
    initialize_runtime || return $?
    log "kubernetes benchmark binaries need no runtime teardown"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/kubernetes/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/kubernetes" ]]; then
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
        stop_kubernetes_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_kubernetes_standalone() {
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
        if build_kubernetes; then
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
        if start_kubernetes_smoke; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_kubernetes_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_kubernetes_runtime; then
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

Clone the official Kubernetes repository at a pinned release, compile its
official go test -bench benchmark binaries hermetically (vendored, network
disabled), and run the benchmark suite as a standalone performance
evaluation.  Results default to results/<version>/<run-id>/ inside this
directory.

Options:
  --version VERSION       Kubernetes release version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  KUBERNETES_REPOSITORY, KUBERNETES_BENCHTIME, GO_VERSION,
  GO_OFFLINE_DIR, GO_RELEASE_URL
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
    run_kubernetes_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
