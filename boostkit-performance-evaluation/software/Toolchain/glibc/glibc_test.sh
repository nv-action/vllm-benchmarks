#!/usr/bin/env bash
# GNU glibc performance case.
#
# Each framework stage uses the same isolated work area: download and build
# the requested GNU release, build its official benchtests, run them, then
# copy their raw output and normalized metrics to RESULTS_DIR. The installed
# system glibc is never replaced or preloaded.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2.31}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
GLIBC_SOURCE_URL="${GLIBC_SOURCE_URL:-https://ftp.gnu.org/gnu/glibc}"
MAKE_SOURCE_URL="https://ftp.gnu.org/gnu/make"
MAKE_VERSION="4.3"
MAKE_ARCHIVE_SHA256="e05fdde47c5f7ca45cb697e973894ff4f5d79e13b750ed57d7b66d8defc78e19"
# Build and run only these two official benchtest executables.
GLIBC_BENCHSET="memcpy memcmp"
GLIBC_BENCHMARKS=(bench-memcpy bench-memcmp)
SRC_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
MAKE_INSTALL_DIR=""
LDCONFIG_BIN=""
GCC_VERSION_STRING=""
TIMING_TYPE_OUTPUT=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[glibc] %s\n' "$*"
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/glibc/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SRC_DIR="${PERF_WORK_DIR}/glibc-src"
    BUILD_DIR="${PERF_WORK_DIR}/glibc-build"
    INSTALL_DIR="${PERF_WORK_DIR}/glibc-install"
    MAKE_INSTALL_DIR="${PERF_WORK_DIR}/make-${MAKE_VERSION}-install"
    LDCONFIG_BIN="${BUILD_DIR}/elf/ldconfig"
    if [[ -x "${MAKE_INSTALL_DIR}/bin/make" &&
          "${PATH}" != "${MAKE_INSTALL_DIR}/bin:"* ]]; then
        PATH="${MAKE_INSTALL_DIR}/bin:${PATH}"
        export PATH
    fi
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    local runner_architecture

    configure_runtime_paths || return $?
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_commands() {
    local required package missing=0
    local packages=()

    for required in gcc make tar xz sha256sum curl python3 awk nproc bison patch; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "missing required command: ${required}"
            missing=1
            case "${required}" in
                gcc) package="gcc" ;;
                make) package="make" ;;
                tar) package="tar" ;;
                xz) package="xz" ;;
                sha256sum|nproc) package="coreutils" ;;
                curl) package="curl" ;;
                python3) package="python3" ;;
                awk) package="gawk" ;;
                bison) package="bison" ;;
                patch) package="patch" ;;
            esac
            packages+=("${package}")
        fi
    done
    if [[ "${missing}" -eq 0 ]]; then
        return 0
    fi

    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install glibc build prerequisites"
        return 30
    fi
    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing glibc build packages: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: sudo is required to install glibc build prerequisites"
        return 30
    elif ! sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install glibc build prerequisites"
        return 30
    fi

    for required in gcc make tar xz sha256sum curl python3 awk nproc bison patch; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required command is still missing after installation: ${required}"
            return 30
        fi
    done
}

prepare_glibc_source() {
    local tarball local_tarball expected_sha256 actual_sha256 source_root
    if [[ -e "${SRC_DIR}" ]]; then
        log "ERROR: source directory already exists: ${SRC_DIR}"
        return 30
    fi
    # The official GNU release checksums are declared beside the download
    # logic: adding a version requires adding its checksum here.
    case "${SOFTWARE_VERSION}" in
        2.31)
            expected_sha256="9246fe44f68feeec8c666bb87973d590ce0137cca145df014c72ec95be9ffd17"
            ;;
        *)
            log "ERROR: no official GNU checksum is declared for glibc ${SOFTWARE_VERSION}"
            return 20
            ;;
    esac
    tarball="${PERF_WORK_DIR}/glibc-${SOFTWARE_VERSION}.tar.xz"
    local_tarball="/home/runner/software/glibc/glibc-${SOFTWARE_VERSION}.tar.xz"
    if [[ -f "${local_tarball}" ]]; then
        tarball="${local_tarball}"
        log "using local glibc source archive ${tarball}"
    else
        log "downloading official glibc ${SOFTWARE_VERSION} release tarball from ${GLIBC_SOURCE_URL}"
        if ! curl -fsSL --retry 3 --connect-timeout 30 \
            -o "${tarball}" \
            "${GLIBC_SOURCE_URL}/glibc-${SOFTWARE_VERSION}.tar.xz"; then
            rm -f "${tarball}"
            log "ERROR: failed to download glibc-${SOFTWARE_VERSION}.tar.xz"
            return 30
        fi
    fi
    actual_sha256="$(sha256sum "${tarball}" | awk '{print $1}')"
    if [[ "${actual_sha256}" != "${expected_sha256}" ]]; then
        log "ERROR: glibc tarball checksum mismatch"
        log "expected: ${expected_sha256}"
        log "actual:   ${actual_sha256}"
        return 30
    fi
    if ! tar -xJf "${tarball}" -C "${PERF_WORK_DIR}"; then
        log "ERROR: failed to extract the glibc tarball"
        return 30
    fi
    source_root="${PERF_WORK_DIR}/glibc-${SOFTWARE_VERSION}"
    if [[ ! -f "${source_root}/configure" ]]; then
        log "ERROR: tarball did not create glibc-${SOFTWARE_VERSION}/configure"
        return 30
    fi
    if ! mv "${source_root}" "${SRC_DIR}"; then
        log "ERROR: failed to move the unpacked source into ${SRC_DIR}"
        return 30
    fi
    # The upstream 2.31 benchtests use TSC ticks on x86_64 and nanoseconds
    # on aarch64. Apply the same measurement-only changes to both builds.
    if ! patch --batch --forward --fuzz=0 -l -p1 -d "${SRC_DIR}" \
        -i "${SCRIPT_DIR}/scripts/glibc-2.31-benchtests.patch"; then
        log "ERROR: failed to apply the glibc 2.31 benchtest measurement patch"
        return 30
    fi
    if [[ "${tarball}" == "${PERF_WORK_DIR}/"* ]]; then
        rm -f "${tarball}"
    fi
}

prepare_compatible_make() {
    local archive="${PERF_WORK_DIR}/make-${MAKE_VERSION}.tar.gz"
    local local_archive="/home/runner/software/glibc/make-${MAKE_VERSION}.tar.gz"
    local source_dir="${PERF_WORK_DIR}/make-${MAKE_VERSION}-src"
    local actual_sha256

    if [[ -f "${local_archive}" ]]; then
        archive="${local_archive}"
        log "using local GNU Make archive ${archive}"
    else
        log "downloading GNU Make ${MAKE_VERSION} from ${MAKE_SOURCE_URL}"
        if ! curl -fsSL --retry 3 --connect-timeout 30 \
            -o "${archive}" "${MAKE_SOURCE_URL}/make-${MAKE_VERSION}.tar.gz"; then
            rm -f "${archive}"
            log "ERROR: failed to download GNU Make ${MAKE_VERSION}"
            return 30
        fi
    fi
    actual_sha256="$(sha256sum "${archive}" | awk '{print $1}')"
    if [[ "${actual_sha256}" != "${MAKE_ARCHIVE_SHA256}" ]]; then
        log "ERROR: GNU Make ${MAKE_VERSION} archive checksum mismatch"
        return 30
    fi
    mkdir -p "${source_dir}"
    if ! tar -xzf "${archive}" -C "${source_dir}" --strip-components=1; then
        log "ERROR: failed to extract GNU Make ${MAKE_VERSION}"
        return 30
    fi
    if [[ "${archive}" == "${PERF_WORK_DIR}/"* ]]; then
        rm -f "${archive}"
    fi
    log "building GNU Make ${MAKE_VERSION} in the private work directory"
    (
        cd "${source_dir}"
        ./configure --prefix="${MAKE_INSTALL_DIR}" --disable-nls
        make -j4
        make install
    ) || {
        log "ERROR: failed to build GNU Make ${MAKE_VERSION}"
        return 30
    }
    if [[ "$("${MAKE_INSTALL_DIR}/bin/make" --version | head -n 1)" != "GNU Make ${MAKE_VERSION}" ]]; then
        log "ERROR: private GNU Make ${MAKE_VERSION} is unavailable"
        return 30
    fi
    PATH="${MAKE_INSTALL_DIR}/bin:${PATH}"
    export PATH
    log "using private $(make --version | head -n 1)"
}

build_glibc() {
    local actual_version
    local version_output

    initialize_runtime || return $?
    require_commands || return $?
    if [[ -e "${SRC_DIR}" || -e "${BUILD_DIR}" || -e "${INSTALL_DIR}" ]]; then
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    prepare_glibc_source || return $?
    prepare_compatible_make || return $?
    GCC_VERSION_STRING="$(gcc --version | head -n 1)"

    log "configuring glibc ${SOFTWARE_VERSION} (out-of-tree build)"
    mkdir -p "${BUILD_DIR}"
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        "${SRC_DIR}/configure" \
            --prefix="${INSTALL_DIR}" \
            --disable-werror \
            --without-selinux \
            CC=gcc CFLAGS=-O3
    ) || {
        log "ERROR: glibc configure failed"
        return 40
    }
    log "building glibc ${SOFTWARE_VERSION} with make -j$(nproc)"
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        # The runner's newer libstdc++ requires symbols absent from glibc 2.31.
        # Use glibc's C implementation of its build-only links-dso helper.
        make -j"$(nproc)" CXX=
    ) || {
        log "ERROR: glibc make build failed"
        return 40
    }
    # Benchtest executables embed the dynamic-loader path below --prefix.
    # Install into this run's isolated prefix so that path exists without
    # touching the runner's system glibc. cross-compiling=yes suppresses
    # glibc's install-time ldconfig update of the system cache.
    log "installing glibc ${SOFTWARE_VERSION} into the isolated prefix ${INSTALL_DIR}"
    (
        cd "${BUILD_DIR}"
        unset LD_LIBRARY_PATH
        make install cross-compiling=yes CXX=
    ) || {
        log "ERROR: glibc installation into ${INSTALL_DIR} failed"
        return 40
    }
    if [[ ! -x "${LDCONFIG_BIN}" ]]; then
        log "ERROR: built ldconfig is missing: ${LDCONFIG_BIN}"
        return 40
    fi
    if ! find "${INSTALL_DIR}/lib" -maxdepth 1 \
        \( -type f -o -type l \) -name 'ld-linux-*.so.*' -print -quit | grep -q .; then
        log "ERROR: installed glibc dynamic loader is missing under ${INSTALL_DIR}/lib"
        return 40
    fi
    if ! version_output="$("${LDCONFIG_BIN}" --version 2>/dev/null | head -n 1)"; then
        log "ERROR: built ldconfig cannot report its version"
        return 40
    fi
    # Output format: "ldconfig (GNU libc) 2.31".
    actual_version="$(printf '%s\n' "${version_output}" | awk '{print $NF}')"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: built glibc reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "glibc ${SOFTWARE_VERSION} build is ready at ${BUILD_DIR}"
}

start_glibc_runtime() {
    initialize_runtime || return $?
    # Each framework stage starts a new shell and therefore inherits the
    # runner environment again.  Do not let host library search paths affect
    # the freshly built glibc benchtest tools.
    unset LD_LIBRARY_PATH
    if [[ "$(make --version | head -n 1)" != "GNU Make ${MAKE_VERSION}" ]]; then
        log "ERROR: private GNU Make ${MAKE_VERSION} is required for glibc 2.31 benchtests"
        return 40
    fi
    if [[ ! -x "${LDCONFIG_BIN}" ]]; then
        log "ERROR: glibc is not built; run the build stage first"
        return 40
    fi
    log "building glibc 2.31 memcpy and memcmp benchtests"
    if ! make -C "${SRC_DIR}/benchtests" objdir="${BUILD_DIR}" \
        "${BUILD_DIR}/benchtests/bench-timing-type" \
        "${BUILD_DIR}/benchtests/bench-memcpy" \
        "${BUILD_DIR}/benchtests/bench-memcmp" CXX=; then
        log "ERROR: glibc memcpy and memcmp benchtest build failed"
        return 40
    fi
    local bench_name
    for bench_name in "${GLIBC_BENCHMARKS[@]}"; do
        if [[ ! -x "${BUILD_DIR}/benchtests/${bench_name}" ]]; then
            log "ERROR: benchtest binary is missing: ${BUILD_DIR}/benchtests/${bench_name}"
            return 40
        fi
    done
    if ! TIMING_TYPE_OUTPUT="$("${BUILD_DIR}/benchtests/bench-timing-type")"; then
        log "ERROR: bench-timing-type is not runnable"
        return 40
    fi
    if [[ "${TIMING_TYPE_OUTPUT}" != "clock_gettime_ns" ]]; then
        log "ERROR: benchtests use unexpected timing backend: ${TIMING_TYPE_OUTPUT}"
        return 40
    fi
    log "benchtests report timing backend: ${TIMING_TYPE_OUTPUT}"
    log "glibc official benchtest runtime is ready"
}

run_glibc_benchmarks() {
    initialize_runtime || return $?
    # See start_glibc_runtime: benchtest binaries must run without a host
    # LD_LIBRARY_PATH.
    unset LD_LIBRARY_PATH
    if [[ ! -x "${LDCONFIG_BIN}" ]]; then
        log "ERROR: glibc is not built; run the build stage first"
        return 50
    fi
    mkdir -p "${RESULTS_DIR}"
    if ! TIMING_TYPE_OUTPUT="$("${BUILD_DIR}/benchtests/bench-timing-type")"; then
        log "ERROR: bench-timing-type is not runnable"
        return 50
    fi
    if [[ "${TIMING_TYPE_OUTPUT}" != "clock_gettime_ns" ]]; then
        log "ERROR: benchtests use unexpected timing backend: ${TIMING_TYPE_OUTPUT}"
        return 50
    fi
    mkdir -p "${RESULTS_DIR}/benchtests"
    : > "${RESULTS_DIR}/benchmark_bench.txt"
    local bench_name output_path
    for bench_name in "${GLIBC_BENCHMARKS[@]}"; do
        output_path="${RESULTS_DIR}/benchtests/${bench_name}.out"
        log "running ${bench_name} with ${TIMING_TYPE_OUTPUT}"
        printf '%s\n' "${BUILD_DIR}/benchtests/${bench_name} > ${output_path}" \
            >> "${RESULTS_DIR}/benchmark_bench.txt"
        if ! "${BUILD_DIR}/benchtests/${bench_name}" > "${output_path}"; then
            log "ERROR: ${bench_name} failed"
            return 50
        fi
        if [[ ! -s "${output_path}" ]]; then
            log "ERROR: ${bench_name} produced empty output"
            return 50
        fi
    done
    export SOFTWARE_VERSION EXPECTED_ARCH GLIBC_BENCHSET TIMING_TYPE_OUTPUT
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/benchtests" \
        "${RESULTS_DIR}/benchmark_glibc.json" || {
        log "ERROR: failed to normalize official glibc benchtest results"
        return 50
    }
    log "benchtest results written to benchmark_bench.txt and benchmark_glibc.json"
}

stop_glibc_runtime() {
    log "glibc benchmark has no background service to stop"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/glibc/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/glibc" ]]; then
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
        stop_glibc_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_glibc_standalone() {
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
        if build_glibc; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
                "${PERF_RUN_ID}" \
                "${GCC_VERSION_STRING}" \
                --source-url="${GLIBC_SOURCE_URL}" \
                --benchset="${GLIBC_BENCHSET}"; then
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
        if start_glibc_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_glibc_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_glibc_runtime; then
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

Build glibc from the official GNU release tarball and run the official
benchtests (make bench) as a standalone performance evaluation. Results
default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       glibc version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  GLIBC_SOURCE_URL
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
    mkdir -p "${RESULTS_DIR}" || return 10
    : > "${RESULTS_DIR}/results.log"
    local pipeline_status=0
    set +e
    run_glibc_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
