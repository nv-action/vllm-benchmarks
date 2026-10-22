#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.14.3}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
FAISS_SOURCE_URL="${FAISS_SOURCE_URL:-https://github.com/facebookresearch/faiss.git}"
FAISS_BENCH_PROFILE="${FAISS_BENCH_PROFILE:-hnsw}"
readonly SRA_SOURCE_URL="https://atomgit.com/liuliuyiyidingding/sra_test.git"
readonly SRA_REVISION="9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf"
readonly DATA_DIRECTORY="/home/runner/software/faiss/data"
readonly MKL_INSTALLER_NAME="intel-onemkl-2026.1.0.237_offline.sh"
readonly MKL_INSTALLER_URL="https://registrationcenter-download.intel.com/akdlm/IRC_NAS/17f37e16-768e-40d2-bcf8-c252dc6c5499/${MKL_INSTALLER_NAME}"
readonly MKL_OFFLINE_DIRECTORY="/home/runner/software/faiss"
readonly ALL_ALGORITHMS=(hnsw ivfpq ivfpqfs pqfs ivfflat ivfrabitq ivfrabitqfs)

SOURCE_DIR=""
BUILD_DIR=""
SRA_DIR=""
MKL_LIBRARY_DIR=""
OPENBLAS_CFLAGS=""
OPENBLAS_LIBS=""
ACTIVE_ALGORITHMS=()
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0


log_message() {
    printf '[faiss] %s\n' "$*"
}


configure_benchmark_profile() {
    case "${FAISS_BENCH_PROFILE}" in
        hnsw) ACTIVE_ALGORITHMS=(hnsw) ;;
        all) ACTIVE_ALGORITHMS=("${ALL_ALGORITHMS[@]}") ;;
        *)
            log_message "ERROR: unsupported Faiss benchmark profile: ${FAISS_BENCH_PROFILE} (expected hnsw or all)"
            return 10
            ;;
    esac
}


normalize_architecture() {
    local architecture
    architecture="${1,,}"
    case "${architecture}" in
        x86_64|amd64)
            printf 'x86_64\n'
            ;;
        aarch64|arm64)
            printf 'aarch64\n'
            ;;
        *)
            printf '%s\n' "${architecture}"
            ;;
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/faiss/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi

    SOURCE_DIR="${PERF_WORK_DIR}/faiss-source"
    BUILD_DIR="${PERF_WORK_DIR}/faiss-build"
    SRA_DIR="${PERF_WORK_DIR}/sra-test"

    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}


initialize_runtime() {
    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" \
        "${TMPDIR}"
}


install_system_packages() {
    local dnf_options=()
    command -v dnf >/dev/null 2>&1 || {
        log_message "ERROR: dnf is required to install Faiss build dependencies"
        return 30
    }
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log_message "installing missing system packages: $*"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "$@" || return 30
    else
        command -v sudo >/dev/null 2>&1 || return 30
        sudo -n dnf "${dnf_options[@]}" install -y "$@" || return 30
    fi
}


require_build_commands() {
    local required_command package
    local cmake_version cmake_major cmake_minor
    local packages=()
    for required_command in git python3 cmake make g++ find nproc tee curl h5dump; do
        command -v "${required_command}" >/dev/null 2>&1 && continue
        case "${required_command}" in
            g++) package="gcc-c++" ;;
            find) package="findutils" ;;
            nproc|tee) package="coreutils" ;;
            h5dump) package="hdf5" ;;
            *) package="${required_command}" ;;
        esac
        log_message "missing required command: ${required_command}"
        packages+=("${package}")
    done
    [[ -f /usr/include/H5Cpp.h || -f /usr/include/hdf5/serial/H5Cpp.h ]] || packages+=(hdf5-devel)
    if ((${#packages[@]})); then
        install_system_packages "${packages[@]}" || return 30
    fi
    for required_command in git python3 cmake make g++ find nproc tee curl h5dump; do
        command -v "${required_command}" >/dev/null 2>&1 || {
            log_message "ERROR: required command remains unavailable: ${required_command}"
            return 30
        }
    done
    [[ -f /usr/include/H5Cpp.h || -f /usr/include/hdf5/serial/H5Cpp.h ]] || {
        log_message "ERROR: HDF5 C++ headers remain unavailable"
        return 30
    }
    cmake_version="$(cmake --version | head -n 1)"
    if [[ ! "${cmake_version}" =~ ([0-9]+)\.([0-9]+) ]]; then
        log_message "ERROR: cannot determine CMake version: ${cmake_version}"
        return 30
    fi
    cmake_major="${BASH_REMATCH[1]}"
    cmake_minor="${BASH_REMATCH[2]}"
    if ((cmake_major < 3 || (cmake_major == 3 && cmake_minor < 24))); then
        log_message "ERROR: Faiss ${SOFTWARE_VERSION} needs CMake >= 3.24; found ${cmake_version}"
        return 30
    fi
    if ! printf '#include <span>\n#include <omp.h>\nint main() { int a[1]={0}; return std::span<int>(a).size() != 1 || omp_get_max_threads() < 1; }\n' |
         g++ -std=c++20 -fopenmp -x c++ - -o "${TMPDIR}/faiss-compiler-check"; then
        log_message "ERROR: g++ must support C++20 and OpenMP for Faiss ${SOFTWARE_VERSION}"
        return 30
    fi
    "${TMPDIR}/faiss-compiler-check" || return 30
}


locate_mkl() {
    local candidate library_dir
    for candidate in "${MKLROOT:-}" "${PERF_WORK_DIR}/oneapi/mkl/latest" \
        "${PERF_WORK_DIR}"/oneapi/mkl/* /opt/intel/oneapi/mkl/latest \
        /opt/intel/oneapi/mkl/*; do
        [[ -n "${candidate}" && -f "${candidate}/include/mkl.h" ]] || continue
        for library_dir in "${candidate}/lib/intel64" "${candidate}/lib"; do
            [[ -d "${library_dir}" ]] || continue
            [[ -e "${library_dir}/libmkl_intel_lp64.so" &&
               -e "${library_dir}/libmkl_gnu_thread.so" &&
               -e "${library_dir}/libmkl_core.so" ]] || continue
            MKLROOT="${candidate}"
            MKL_LIBRARY_DIR="${library_dir}"
            export MKLROOT
            return 0
        done
    done
    return 1
}


prepare_math_library() {
    local installer instance_id
    local openblas_flags=()
    if [[ "$(normalize_architecture "${EXPECTED_ARCH}")" == "aarch64" ]]; then
        if ! command -v pkg-config >/dev/null 2>&1; then
            install_system_packages pkgconf-pkg-config || return 30
        fi
        if ! pkg-config --exists openblas; then
            install_system_packages openblas-devel || return 30
        fi
        if ! pkg-config --exists openblas; then
            log_message "ERROR: system OpenBLAS development files are unavailable"
            return 30
        fi
        OPENBLAS_CFLAGS="$(pkg-config --cflags openblas)"
        OPENBLAS_LIBS="$(pkg-config --libs openblas)"
        read -r -a openblas_flags <<< "${OPENBLAS_CFLAGS} ${OPENBLAS_LIBS}"
        if ! printf '#include <cblas.h>\nint main() { double x[1]={1}; return cblas_ddot(1,x,1,x,1) != 1; }\n' |
             g++ -std=c++17 -fopenmp -x c++ - "${openblas_flags[@]}" \
                 -o "${TMPDIR}/faiss-blas-check" ||
           ! "${TMPDIR}/faiss-blas-check"; then
            log_message "ERROR: system OpenBLAS headers or libraries cannot be linked and run"
            return 30
        fi
        log_message "using system OpenBLAS on aarch64"
        return 0
    fi

    if ! locate_mkl; then
        installer="${MKL_OFFLINE_DIRECTORY}/${MKL_INSTALLER_NAME}"
        if [[ ! -s "${installer}" ]]; then
            installer="${PERF_WORK_DIR}/${MKL_INSTALLER_NAME}"
            log_message "downloading official Intel oneMKL offline installer"
            if ! curl --fail --location --retry 3 --output "${installer}" "${MKL_INSTALLER_URL}"; then
                log_message "ERROR: cannot download Intel oneMKL; place ${MKL_INSTALLER_NAME} in ${MKL_OFFLINE_DIRECTORY}"
                return 30
            fi
        else
            log_message "using local Intel oneMKL offline installer ${installer}"
        fi
        instance_id="faiss-${PERF_RUN_ID//[^a-zA-Z0-9]/-}"
        log_message "installing Intel oneMKL into private work directory (instance ${instance_id})"
        if ! sh "${installer}" -a --silent --eula accept \
            --install-dir="${PERF_WORK_DIR}/oneapi" --instance="${instance_id}"; then
            log_message "ERROR: Intel oneMKL offline installation failed"
            return 30
        fi
        if ! locate_mkl; then
            log_message "ERROR: Intel oneMKL installation did not provide its headers and GNU OpenMP libraries"
            return 30
        fi
    fi
    if ! printf '#include <mkl.h>\nint main() { double x[1]={1}; return cblas_ddot(1,x,1,x,1) != 1; }\n' |
         g++ -std=c++17 -fopenmp -x c++ - -I"${MKLROOT}/include" \
             -L"${MKL_LIBRARY_DIR}" -Wl,-rpath,"${MKL_LIBRARY_DIR}" \
             -Wl,--no-as-needed -lmkl_intel_lp64 -lmkl_gnu_thread -lmkl_core \
             -lgomp -lpthread -lm -ldl -o "${TMPDIR}/faiss-blas-check" ||
       ! "${TMPDIR}/faiss-blas-check"; then
        log_message "ERROR: Intel oneMKL headers or libraries cannot be linked and run"
        return 30
    fi
    log_message "using Intel oneMKL at ${MKLROOT}"
}


check_architecture() {
    local actual_architecture
    local expected_architecture
    actual_architecture="$(normalize_architecture "$(uname -m)")"
    expected_architecture="$(normalize_architecture "${EXPECTED_ARCH}")"
    if [[ "${actual_architecture}" != "${expected_architecture}" ]]; then
        log_message "ERROR: expected ${expected_architecture}, runner is ${actual_architecture}"
        return 20
    fi
}


build_faiss() {
    local source_tag
    local actual_version
    local parallel_jobs
    local faiss_library
    local config_file
    local algorithm
    local faiss_mkl_mode
    local faiss_link_file
    local expected_blas

    if initialize_runtime; then
        :
    else
        return $?
    fi
    if ! configure_benchmark_profile; then
        return 10
    fi
    if check_architecture; then
        :
    else
        return $?
    fi
    if require_build_commands; then
        :
    else
        return $?
    fi
    if prepare_math_library; then
        :
    else
        return $?
    fi
    if [[ -e "${SOURCE_DIR}" || -e "${BUILD_DIR}" || -e "${SRA_DIR}" ]]; then
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi

    source_tag="${SOFTWARE_VERSION}"
    if [[ "${source_tag}" != v* ]]; then
        source_tag="v${source_tag}"
    fi
    export GIT_TERMINAL_PROMPT=0
    log_message "cloning Faiss ${source_tag} from ${FAISS_SOURCE_URL}"
    if ! git clone --branch "${source_tag}" --depth 1 \
        "${FAISS_SOURCE_URL}" "${SOURCE_DIR}"; then
        log_message "ERROR: failed to clone Faiss ${source_tag}"
        return 30
    fi

    actual_version="$(git -C "${SOURCE_DIR}" describe --tags --exact-match 2>/dev/null || true)"
    [[ "${actual_version}" == "${source_tag}" ]] || {
        log_message "ERROR: Faiss source tag is ${actual_version:-missing}, expected ${source_tag}"
        return 40
    }
    faiss_mkl_mode=OFF
    if [[ "$(normalize_architecture "${EXPECTED_ARCH}")" == "x86_64" ]]; then
        faiss_mkl_mode=ON
    fi
    log_message "configuring CPU-only Faiss ${SOFTWARE_VERSION} shared library"
    if ! cmake -B "${BUILD_DIR}" -S "${SOURCE_DIR}" \
        -DFAISS_ENABLE_GPU=OFF \
        -DFAISS_ENABLE_PYTHON=OFF \
        -DFAISS_ENABLE_EXTRAS=OFF \
        -DFAISS_ENABLE_MKL="${faiss_mkl_mode}" \
        -DBUILD_TESTING=OFF \
        -DBUILD_SHARED_LIBS=ON \
        -DCMAKE_BUILD_TYPE=Release; then
        log_message "ERROR: Faiss CMake configuration failed"
        return 40
    fi
    faiss_link_file="${BUILD_DIR}/faiss/CMakeFiles/faiss.dir/link.txt"
    [[ -f "${faiss_link_file}" ]] || {
        log_message "ERROR: Faiss CMake link command is unavailable: ${faiss_link_file}"
        return 40
    }
    expected_blas=openblas
    [[ "${faiss_mkl_mode}" == OFF ]] || expected_blas=mkl
    if [[ "$(<"${faiss_link_file}")" != *"${expected_blas}"* ]]; then
        log_message "ERROR: Faiss is not linked with expected ${expected_blas} math library"
        return 40
    fi

    parallel_jobs="$(nproc)"
    log_message "building official Faiss C++ library"
    if ! make -C "${BUILD_DIR}" -j"${parallel_jobs}" faiss; then
        log_message "ERROR: Faiss C++ library build failed"
        return 40
    fi
    faiss_library="$(find "${BUILD_DIR}" -type f -name 'libfaiss.so' -print -quit)"
    if [[ -z "${faiss_library}" ]]; then
        log_message "ERROR: built libfaiss.so is unavailable"
        return 40
    fi
    log_message "fetching sra_test at ${SRA_REVISION}"
    if ! git init -q "${SRA_DIR}" || \
       ! git -C "${SRA_DIR}" remote add origin "${SRA_SOURCE_URL}" || \
       ! git -C "${SRA_DIR}" fetch --depth 1 --filter=blob:none origin "${SRA_REVISION}" || \
       ! git -C "${SRA_DIR}" sparse-checkout set scripts configs include src || \
       ! git -C "${SRA_DIR}" checkout --detach FETCH_HEAD; then
        log_message "ERROR: failed to fetch pinned sra_test source"
        return 30
    fi
    if [[ "$(git -C "${SRA_DIR}" rev-parse HEAD)" != "${SRA_REVISION}" ]]; then
        log_message "ERROR: sra_test revision mismatch"
        return 40
    fi
    mkdir -p "${SRA_DIR}/build"
    config_file="${SRA_DIR}/build/config_faiss_$(normalize_architecture "${EXPECTED_ARCH}").sh"
    {
        printf 'export FAISS_SO=%q\n' "${faiss_library}"
        printf 'export FAISS_LIB_DIR=%q\n' "$(dirname "${faiss_library}")"
        printf 'export FAISS_LIBNAME=faiss\n'
        printf 'export FAISS_INC=%q\n' "${SOURCE_DIR}"
        printf 'export EXTRA_DEFINES=""\n'
        if [[ "$(normalize_architecture "${EXPECTED_ARCH}")" == "x86_64" ]]; then
            printf 'export MKLROOT=%q\n' "${MKLROOT}"
            printf 'export MKL_CFLAGS=%q\n' "-I${MKLROOT}/include"
            printf 'export MKL_LIBS=%q\n' "-L${MKL_LIBRARY_DIR} -Wl,-rpath,${MKL_LIBRARY_DIR} -Wl,--no-as-needed -lmkl_intel_lp64 -lmkl_gnu_thread -lmkl_core -lgomp -lpthread -lm -ldl"
        else
            printf 'export OPENBLAS_CFLAGS=%q\n' "${OPENBLAS_CFLAGS}"
            printf 'export OPENBLAS_LIBS=%q\n' "${OPENBLAS_LIBS}"
        fi
    } > "${config_file}"
    for algorithm in "${ACTIVE_ALGORITHMS[@]}"; do
        log_message "building original sra_test ${algorithm}_test"
        if ! (cd "${SRA_DIR}" && printf '\n' | make "${algorithm}_test"); then
            log_message "ERROR: sra_test ${algorithm}_test build failed"
            return 40
        fi
    done
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    if ! printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"; then
        log_message "ERROR: failed to record built Faiss version"
        return 40
    fi
}


start_faiss_runtime() {
    local algorithm
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if ! configure_benchmark_profile; then
        return 10
    fi
    for algorithm in "${ACTIVE_ALGORITHMS[@]}"; do
        [[ -x "${SRA_DIR}/${algorithm}_test" ]] || {
            log_message "ERROR: sra_test ${algorithm} benchmark is unavailable"
            return 40
        }
    done
    log_message "sra_test Faiss C++ benchmark binaries are ready"
}


run_faiss_benchmarks() {
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if ! configure_benchmark_profile; then
        return 10
    fi
    if ! python3 "${SCRIPT_DIR}/scripts/run_sra_benchmark.py" \
        --source "${SRA_DIR}" --data "${DATA_DIRECTORY}" \
        --results "${RESULTS_DIR}" --version "${SOFTWARE_VERSION}" \
        --architecture "$(normalize_architecture "${EXPECTED_ARCH}")" \
        --algorithms "${ACTIVE_ALGORITHMS[@]}"; then
        log_message "ERROR: sra_test Faiss benchmark failed"
        return 50
    fi
}


stop_faiss_runtime() {
    log_message "Faiss CPU benchmark has no background service to stop"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/faiss/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/faiss" ]]; then
        log_message "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        if ! rm -rf -- "${PERF_WORK_DIR}"; then
            log_message "ERROR: failed to clean standalone work directory"
            return 70
        fi
    fi
    log_message "cleaned standalone work directory: ${PERF_WORK_DIR}"
}


emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_faiss_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}


run_faiss_standalone() {
    local stage_status=0
    local failed_stage=""
    local cleanup_status="passed"
    local command_status="passed"
    local finalize_status=0

    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    STANDALONE_STOP_DONE=0
    STANDALONE_CLEANUP_DONE=0
    trap emergency_standalone_cleanup EXIT
    if initialize_runtime; then
        :
    else
        return $?
    fi

    if standalone_runtime system "${RESULTS_DIR}/system_info.json"; then
        if standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
            :
        else
            stage_status=$?
            failed_stage="prepare"
        fi
    else
        stage_status=$?
        failed_stage="prepare"
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if build_faiss; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_architecture "${EXPECTED_ARCH}")" \
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
        if start_faiss_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_faiss_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_faiss_runtime; then
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
        :
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

Build Faiss CPU from source, run original sra_test benchmarks, collect environment
information, validate results, generate a report, and clean the private work area.

Options:
  --version VERSION       Faiss version (default: ${SOFTWARE_VERSION})
  --profile PROFILE       hnsw (default) or all
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR, FAISS_SOURCE_URL,
  FAISS_BENCH_PROFILE, PERF_PROXY, MKLROOT
USAGE
}


main() {
    local pipeline_status
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version)
                if [[ "$#" -lt 2 ]]; then
                    log_message "ERROR: --version requires a value"
                    return 10
                fi
                SOFTWARE_VERSION="$2"
                shift 2
                ;;
            --results-dir)
                if [[ "$#" -lt 2 ]]; then
                    log_message "ERROR: --results-dir requires a value"
                    return 10
                fi
                RESULTS_DIR="$2"
                shift 2
                ;;
            --profile)
                if [[ "$#" -lt 2 ]]; then
                    log_message "ERROR: --profile requires hnsw or all"
                    return 10
                fi
                FAISS_BENCH_PROFILE="$2"
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

    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    mkdir -p "${RESULTS_DIR}"
    : > "${RESULTS_DIR}/results.log"
    pipeline_status=0
    set +e
    run_faiss_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}


if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
