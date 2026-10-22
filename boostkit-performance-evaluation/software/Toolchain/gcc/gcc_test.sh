#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-14.4.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
GCC_SOURCE_BASE="${GCC_SOURCE_BASE:-https://gcc.gnu.org/pub/gcc/releases}"
GCC_OFFLINE_DIR="${GCC_OFFLINE_DIR:-/home/runner/software/gcc}"
GCC_BENCHMARK_DATA_ROOT="${GCC_BENCHMARK_DATA_ROOT:-}"
SPEC_CPU2017_ISO="${SPEC_CPU2017_ISO:-/home/runner/software/gcc/cpu2017-1.0.5.iso}"
SPEC_CONFIG_NAME=""
SPEC_COPIES=384

SRC_DIR=""
BUILD_DIR=""
INSTALL_DIR=""
GCC_BIN=""
GXX_BIN=""
GFORTRAN_BIN=""
GCC_VERSION_STRING=""
GCC_SOURCE_SHA256=""
BENCHMARK_DATA_DIR=""
SPEC_MOUNT_DIR=""
SPEC_DIR=""
SPEC_OUTPUT_ROOT=""
SPEC_RESULT_DIR=""
SPEC_CONFIG_PATH=""
ASLR_STATE_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[gcc] %s\n' "$*"
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
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64)
            if [[ "${EXPECTED_ARCH}" != "x86_64" ]]; then
                log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is x86_64"
                return 20
            fi
            ;;
        aarch64|arm64)
            if [[ "${EXPECTED_ARCH}" != "aarch64" ]]; then
                log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is aarch64"
                return 20
            fi
            ;;
        *)
            log "ERROR: unsupported runner architecture: $(uname -m)"
            return 20
            ;;
    esac
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/gcc/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    if [[ -z "${GCC_BENCHMARK_DATA_ROOT}" ]]; then
        GCC_BENCHMARK_DATA_ROOT="${PERF_WORK_DIR}/data"
    fi
    SRC_DIR="${PERF_WORK_DIR}/gcc-src"
    BUILD_DIR="${PERF_WORK_DIR}/gcc-build"
    INSTALL_DIR="${PERF_WORK_DIR}/gcc-install"
    GCC_BIN="${INSTALL_DIR}/bin/gcc"
    GXX_BIN="${INSTALL_DIR}/bin/g++"
    GFORTRAN_BIN="${INSTALL_DIR}/bin/gfortran"
    BENCHMARK_DATA_DIR="${GCC_BENCHMARK_DATA_ROOT%/}/gcc-${PERF_RUN_ID}"
    SPEC_MOUNT_DIR="${BENCHMARK_DATA_DIR}/cpu2017-media"
    SPEC_DIR="${BENCHMARK_DATA_DIR}/cpu2017"
    if [[ "${EXPECTED_ARCH}" == "x86_64" ]]; then
        SPEC_CONFIG_NAME="spec-gcc-x86.cfg"
    else
        SPEC_CONFIG_NAME="spec-gcc-aarch64.cfg"
    fi
    SPEC_CONFIG_PATH="${SPEC_DIR}/config/${SPEC_CONFIG_NAME}"
    ASLR_STATE_FILE="${BENCHMARK_DATA_DIR}/randomize_va_space.before"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

install_dependencies() {
    local required missing=0
    local required_commands=(gcc g++ gfortran make tar xz sha256sum curl python3 awk date sort nproc grep perl mount umount numactl)
    local packages=(gcc gcc-c++ gcc-gfortran make tar xz coreutils curl python3 grep
        gawk findutils gmp-devel mpfr-devel libmpc-devel bison flex perl util-linux libnsl numactl)
    for required in "${required_commands[@]}"; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            missing=1
        fi
    done
    if [[ "${missing}" -eq 0 ]] && printf \
        '#include <gmp.h>\n#include <mpfr.h>\n#include <mpc.h>\nint main(void) { return 0; }\n' | \
        g++ -x c++ -fsyntax-only - >/dev/null 2>&1 && \
        ldconfig -p 2>/dev/null | grep -q 'libnsl\.so\.1'; then
        return
    fi

    log "installing missing GCC build dependencies"
    local package_manager_options=()
    [[ -z "${PERF_PROXY:-}" ]] || package_manager_options+=("--setopt=proxy=${PERF_PROXY}")
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install GCC build dependencies"
        return 30
    fi
    local install_command=(dnf)
    if [[ "${EUID}" -ne 0 ]]; then
        if ! command -v sudo >/dev/null 2>&1; then
            log "ERROR: root privileges are required to install GCC build dependencies"
            return 30
        fi
        install_command=(sudo -n dnf)
    fi
    if ! "${install_command[@]}" "${package_manager_options[@]}" install -y "${packages[@]}"; then
        log "ERROR: failed to install GCC build dependencies"
        return 30
    fi

    for required in "${required_commands[@]}"; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required command is still missing after installation: ${required}"
            return 30
        fi
    done
    if ! printf '#include <gmp.h>\n#include <mpfr.h>\n#include <mpc.h>\nint main(void) { return 0; }\n' | \
        g++ -x c++ -fsyntax-only - >/dev/null 2>&1; then
        log "ERROR: GMP, MPFR, or MPC development headers are unavailable after installation"
        return 30
    fi
    if ! ldconfig -p 2>/dev/null | grep -q 'libnsl\.so\.1'; then
        log "ERROR: SPEC CPU2017 tools require libnsl.so.1, but libnsl did not provide it"
        return 30
    fi
}

prepare_gcc_source() {
    local tarball offline_tarball actual_sha256 source_root

    case "${SOFTWARE_VERSION}" in
        14.4.0)
            GCC_SOURCE_SHA256="752b6f567beac83159c77a7680b1316bdd784738bff9a9d070112c09da90f6d9"
            ;;
        *)
            log "ERROR: unsupported GCC version: ${SOFTWARE_VERSION}"
            return 20
            ;;
    esac

    if [[ -e "${SRC_DIR}" ]]; then
        log "ERROR: source directory already exists: ${SRC_DIR}"
        return 30
    fi
    tarball="${PERF_WORK_DIR}/gcc-${SOFTWARE_VERSION}.tar.xz"
    offline_tarball="${GCC_OFFLINE_DIR}/gcc-${SOFTWARE_VERSION}.tar.xz"
    if [[ -f "${offline_tarball}" ]]; then
        log "using local GCC archive ${offline_tarball}"
        if ! cp "${offline_tarball}" "${tarball}"; then
            log "ERROR: failed to copy local GCC archive"
            return 30
        fi
    else
        log "downloading official gcc ${SOFTWARE_VERSION} release tarball from ${GCC_SOURCE_BASE}"
        if ! curl -fsSL --retry 3 --connect-timeout 30 \
            -o "${tarball}" \
            "${GCC_SOURCE_BASE}/gcc-${SOFTWARE_VERSION}/gcc-${SOFTWARE_VERSION}.tar.xz"; then
            log "ERROR: failed to download gcc-${SOFTWARE_VERSION}.tar.xz"
            return 30
        fi
    fi
    actual_sha256="$(sha256sum "${tarball}" | awk '{print $1}')"
    if [[ "${actual_sha256}" != "${GCC_SOURCE_SHA256}" ]]; then
        log "ERROR: GCC tarball checksum mismatch"
        log "expected: ${GCC_SOURCE_SHA256}"
        log "actual:   ${actual_sha256}"
        return 30
    fi
    log "extracting GCC source (checksum verified)"
    if ! tar -xJf "${tarball}" -C "${PERF_WORK_DIR}"; then
        log "ERROR: failed to extract the GCC tarball"
        return 30
    fi
    source_root="${PERF_WORK_DIR}/gcc-${SOFTWARE_VERSION}"
    if [[ ! -f "${source_root}/configure" || \
          ! -d "${source_root}/gcc/testsuite/gcc.c-torture/compile" ]]; then
        log "ERROR: tarball did not create the expected GCC source tree"
        return 30
    fi
    if ! mv "${source_root}" "${SRC_DIR}"; then
        log "ERROR: failed to place the extracted GCC source tree"
        return 30
    fi
    rm -f "${tarball}"
}

build_gcc() {
    local version_output actual_version

    if initialize_runtime; then
        :
    else
        return $?
    fi
    if install_dependencies; then
        :
    else
        return $?
    fi
    if [[ -e "${SRC_DIR}" || -e "${BUILD_DIR}" || -e "${INSTALL_DIR}" ]]; then
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    if prepare_gcc_source; then
        :
    else
        return $?
    fi

    log "configuring GCC ${SOFTWARE_VERSION} (C and C++, out-of-tree build)"
    mkdir -p "${BUILD_DIR}"
    if ! (
        cd "${BUILD_DIR}"
        "${SRC_DIR}/configure" \
            --prefix="${INSTALL_DIR}" \
            --enable-languages=c,c++,fortran \
            --disable-bootstrap \
            --disable-multilib \
            --disable-nls
    ); then
        log "ERROR: GCC configure failed"
        return 40
    fi
    log "building GCC ${SOFTWARE_VERSION} with make -j$(nproc)"
    if ! (
        cd "${BUILD_DIR}"
        make -j"$(nproc)"
    ); then
        log "ERROR: GCC build failed"
        return 40
    fi
    log "installing GCC ${SOFTWARE_VERSION} into ${INSTALL_DIR}"
    if ! (
        cd "${BUILD_DIR}"
        make install
    ); then
        log "ERROR: GCC private installation failed"
        return 40
    fi
    if [[ ! -x "${GCC_BIN}" || ! -x "${GXX_BIN}" || ! -x "${GFORTRAN_BIN}" ]]; then
        log "ERROR: private GCC C, C++, or Fortran compiler is missing"
        return 40
    fi
    if ! version_output="$("${GCC_BIN}" --version 2>/dev/null | head -n 1)"; then
        log "ERROR: built GCC cannot report its version"
        return 40
    fi
    actual_version="$(printf '%s\n' "${version_output}" | awk '{print $NF}')"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: built GCC reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    fi
    GCC_VERSION_STRING="${version_output}"
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "GCC ${SOFTWARE_VERSION} compiler is ready at ${GCC_BIN}"
}

start_gcc_runtime() {
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if [[ ! -x "${GCC_BIN}" || ! -x "${GXX_BIN}" || ! -x "${GFORTRAN_BIN}" ]]; then
        log "ERROR: private GCC C, C++, or Fortran compiler is missing"
        return 40
    fi
    if [[ ! -r "${SPEC_CPU2017_ISO}" ]]; then
        log "ERROR: SPEC CPU2017 ISO is missing or unreadable: ${SPEC_CPU2017_ISO}"
        return 40
    fi
    if [[ "${EUID}" -ne 0 ]] && ! sudo -n true >/dev/null 2>&1; then
        log "ERROR: passwordless sudo is required to mount the SPEC CPU2017 ISO"
        return 40
    fi
    if [[ ! "${SPEC_COPIES}" =~ ^[1-9][0-9]*$ ]]; then
        log "ERROR: SPEC_COPIES must be a positive integer: ${SPEC_COPIES}"
        return 40
    fi
    log "SPEC CPU2017 ISO is ready: ${SPEC_CPU2017_ISO}"
}

preserve_spec_build_logs() {
    local build_log relative_path destination

    if [[ ! -d "${SPEC_OUTPUT_ROOT}/benchspec/CPU" ]]; then
        return
    fi
    while IFS= read -r -d '' build_log; do
        relative_path="${build_log#"${SPEC_DIR}/"}"
        destination="${RESULTS_DIR}/spec-build-logs/${relative_path}"
        if ! mkdir -p "$(dirname "${destination}")" || \
           ! cp "${build_log}" "${destination}"; then
            log "ERROR: failed to preserve SPEC build log: ${build_log}"
            return 50
        fi
    done < <(find "${SPEC_OUTPUT_ROOT}/benchspec/CPU" -type f -name 'make*.out' -print0)
}

run_gcc_benchmarks() {
    local actual_version config_source spec_label spec_group

    if initialize_runtime; then
        :
    else
        return $?
    fi
    if [[ ! -x "${GCC_BIN}" || ! -x "${GXX_BIN}" || ! -x "${GFORTRAN_BIN}" ]]; then
        log "ERROR: private GCC C, C++, or Fortran compiler is missing"
        return 40
    fi
    if ! GCC_VERSION_STRING="$("${GCC_BIN}" --version 2>/dev/null | head -n 1)"; then
        log "ERROR: built GCC cannot report its version"
        return 40
    fi
    actual_version="$(printf '%s\n' "${GCC_VERSION_STRING}" | awk '{print $NF}')"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}" ]]; then
        log "ERROR: built GCC reports ${actual_version}, requested ${SOFTWARE_VERSION}"
        return 40
    fi
    if [[ -L "${BENCHMARK_DATA_DIR}" ]]; then
        log "ERROR: GCC benchmark data directory must not be a symlink: ${BENCHMARK_DATA_DIR}"
        return 50
    fi
    if [[ -e "${BENCHMARK_DATA_DIR}" ]]; then
        if [[ ! -f "${BENCHMARK_DATA_DIR}/.gcc-run-id" ]] || \
            [[ "$(<"${BENCHMARK_DATA_DIR}/.gcc-run-id")" != "${PERF_RUN_ID}" ]]; then
            log "ERROR: GCC benchmark data directory belongs to another run: ${BENCHMARK_DATA_DIR}"
            return 50
        fi
    else
        mkdir -p "${BENCHMARK_DATA_DIR}"
        printf '%s\n' "${PERF_RUN_ID}" > "${BENCHMARK_DATA_DIR}/.gcc-run-id"
    fi
    mkdir -p "${RESULTS_DIR}" "${SPEC_MOUNT_DIR}"
    if [[ "${EUID}" -eq 0 ]]; then
        if ! mount -o loop,ro "${SPEC_CPU2017_ISO}" "${SPEC_MOUNT_DIR}"; then
            log "ERROR: failed to mount the SPEC CPU2017 ISO"
            return 50
        fi
    elif ! sudo -n mount -o loop,ro "${SPEC_CPU2017_ISO}" "${SPEC_MOUNT_DIR}"; then
        log "ERROR: failed to mount the SPEC CPU2017 ISO"
        return 50
    fi
    if ! (
        cd "${SPEC_MOUNT_DIR}"
        env -u SPEC ./install.sh -f -d "${SPEC_DIR}"
    ) 2>&1 | tee "${RESULTS_DIR}/spec-install.log"; then
        log "ERROR: SPEC CPU2017 installation failed"
        return 50
    fi
    if [[ ! -x "${SPEC_DIR}/bin/runcpu" || ! -x "${SPEC_DIR}/bin/specperl" ]]; then
        log "ERROR: SPEC CPU2017 installation is incomplete"
        return 50
    fi
    config_source="${SCRIPT_DIR}/scripts/${SPEC_CONFIG_NAME}"
    if [[ ! -f "${config_source}" ]]; then
        log "ERROR: SPEC CPU2017 GCC configuration is missing: ${config_source}"
        return 50
    fi
    if ! sed \
        -e "s|^%   define  gcc_dir[[:space:]].*$|%   define  gcc_dir        ${INSTALL_DIR}|" \
        "${config_source}" > "${SPEC_CONFIG_PATH}"; then
        log "ERROR: failed to place the SPEC CPU2017 GCC configuration"
        return 50
    fi
    if ! grep -Fxq "%   define  gcc_dir        ${INSTALL_DIR}" "${SPEC_CONFIG_PATH}"; then
        log "ERROR: SPEC CPU2017 GCC compiler path was not updated"
        return 50
    fi
    spec_label="$(awk '$1 == "%" && $2 == "define" && $3 == "label" {print $4; exit}' "${SPEC_CONFIG_PATH}")"
    spec_group="$(awk '$1 == "%" && $2 == "define" && $3 == "group" {print $4; exit}' "${SPEC_CONFIG_PATH}")"
    if [[ ! "${spec_label}" =~ ^[A-Za-z0-9._-]+$ || ! "${spec_group}" =~ ^[A-Za-z0-9._-]+$ ]] || \
        ! grep -Fqx 'output_root          = %{top}/result/%{group}/%{label}' "${SPEC_CONFIG_PATH}"; then
        log "ERROR: unsupported SPEC CPU2017 output_root configuration"
        return 50
    fi
    SPEC_OUTPUT_ROOT="${SPEC_DIR}/result/${spec_group}/${spec_label}"
    SPEC_RESULT_DIR="${SPEC_OUTPUT_ROOT}/result"
    if ! cp "${SPEC_CONFIG_PATH}" "${RESULTS_DIR}/${SPEC_CONFIG_NAME}"; then
        log "ERROR: failed to preserve the SPEC CPU2017 GCC configuration"
        return 50
    fi
    if ! cat /proc/sys/kernel/randomize_va_space > "${ASLR_STATE_FILE}"; then
        log "ERROR: failed to record the current ASLR setting"
        return 50
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        if ! printf '0\n' > /proc/sys/kernel/randomize_va_space || \
            ! printf '3\n' > /proc/sys/vm/drop_caches; then
            log "ERROR: failed to prepare the requested SPEC CPU2017 system settings"
            return 50
        fi
    elif ! printf '0\n' | sudo -n tee /proc/sys/kernel/randomize_va_space >/dev/null || \
        ! printf '3\n' | sudo -n tee /proc/sys/vm/drop_caches >/dev/null; then
        log "ERROR: failed to prepare the requested SPEC CPU2017 system settings"
        return 50
    fi
    log "running SPEC CPU2017 intrate: ${SPEC_COPIES} copies, one iteration"
    if ! (
        cd "${SPEC_DIR}"
        source ./shrc
        runcpu --config="${SPEC_CONFIG_NAME}" intrate -n 1 -C "${SPEC_COPIES}"
    ) 2>&1 | tee "${RESULTS_DIR}/raw-output.log"; then
        preserve_spec_build_logs || return $?
        log "ERROR: SPEC CPU2017 intrate failed"
        return 50
    fi
    if awk '
        /^Build errors for intrate:/ && $0 != "Build errors for intrate: None" {
            has_build_errors = 1
        }
        END { exit !has_build_errors }
    ' "${RESULTS_DIR}/raw-output.log"; then
        preserve_spec_build_logs || return $?
        log "ERROR: SPEC CPU2017 intrate reported benchmark build errors"
        return 50
    fi
    if [[ ! -d "${SPEC_RESULT_DIR}" ]]; then
        log "ERROR: SPEC CPU2017 result directory is missing"
        return 50
    fi
    if ! mkdir -p "${RESULTS_DIR}/spec-results" || \
        ! cp -a "${SPEC_RESULT_DIR}/." "${RESULTS_DIR}/spec-results/"; then
        log "ERROR: failed to preserve SPEC CPU2017 result files"
        return 50
    fi
    export SOFTWARE_VERSION EXPECTED_ARCH GCC_VERSION_STRING SPEC_COPIES SPEC_CONFIG_NAME
    if ! python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/spec-results" \
        "${RESULTS_DIR}/benchmark_gcc.json"; then
        log "ERROR: failed to normalize the SPEC CPU2017 results"
        return 50
    fi
    log "SPEC CPU2017 results written to raw-output.log, spec-results, and benchmark_gcc.json"
}

stop_gcc_runtime() {
    local cleanup_failed=0 previous_aslr
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if [[ -f "${ASLR_STATE_FILE}" ]]; then
        previous_aslr="$(<"${ASLR_STATE_FILE}")"
        if [[ ! "${previous_aslr}" =~ ^[0-9]+$ ]]; then
            log "ERROR: recorded ASLR setting is invalid: ${previous_aslr}"
            cleanup_failed=1
        elif [[ "${EUID}" -eq 0 ]]; then
            if ! printf '%s\n' "${previous_aslr}" > /proc/sys/kernel/randomize_va_space; then
                log "ERROR: failed to restore the ASLR setting"
                cleanup_failed=1
            fi
        elif ! printf '%s\n' "${previous_aslr}" | sudo -n tee /proc/sys/kernel/randomize_va_space >/dev/null; then
            log "ERROR: failed to restore the ASLR setting"
            cleanup_failed=1
        fi
    fi
    if grep -F " ${SPEC_MOUNT_DIR} " /proc/mounts >/dev/null 2>&1; then
        if [[ "${EUID}" -eq 0 ]]; then
            if ! umount "${SPEC_MOUNT_DIR}"; then
                log "ERROR: failed to unmount the SPEC CPU2017 ISO"
                cleanup_failed=1
            fi
        elif ! sudo -n umount "${SPEC_MOUNT_DIR}"; then
            log "ERROR: failed to unmount the SPEC CPU2017 ISO"
            cleanup_failed=1
        fi
    fi
    if [[ -e "${BENCHMARK_DATA_DIR}" || -L "${BENCHMARK_DATA_DIR}" ]]; then
        if [[ -L "${BENCHMARK_DATA_DIR}" || ! -d "${BENCHMARK_DATA_DIR}" ]] || \
            [[ "${BENCHMARK_DATA_DIR}" != "${GCC_BENCHMARK_DATA_ROOT%/}/gcc-${PERF_RUN_ID}" ]] || \
            [[ ! -f "${BENCHMARK_DATA_DIR}/.gcc-run-id" ]] || \
            [[ "$(<"${BENCHMARK_DATA_DIR}/.gcc-run-id")" != "${PERF_RUN_ID}" ]]; then
            log "ERROR: refusing to clean unverified GCC benchmark data directory: ${BENCHMARK_DATA_DIR}"
            cleanup_failed=1
        fi
        if [[ "${cleanup_failed}" -eq 0 ]] && ! rm -rf -- "${BENCHMARK_DATA_DIR}"; then
            log "ERROR: failed to clean GCC benchmark data directory: ${BENCHMARK_DATA_DIR}"
            cleanup_failed=1
        fi
    fi
    if [[ "${cleanup_failed}" -ne 0 ]]; then
        return 70
    fi
    log "cleaned the private SPEC CPU2017 installation and benchmark data"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/gcc/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/gcc" ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    if [[ -d "${PERF_WORK_DIR}" ]]; then
        if ! rm -rf -- "${PERF_WORK_DIR}"; then
            log "ERROR: failed to clean standalone work directory: ${PERF_WORK_DIR}"
            return 70
        fi
    fi
    log "cleaned standalone work directory: ${PERF_WORK_DIR}"
}

emergency_standalone_cleanup() {
    set +e
    if [[ "${STANDALONE_STOP_DONE}" -ne 1 ]]; then
        stop_gcc_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_gcc_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed" finalize_status=0
    local command_status="passed"

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

    if standalone_runtime system "${RESULTS_DIR}/system_info.json" && \
        standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"; then
        :
    else
        stage_status=$?
        failed_stage="prepare"
    fi

    if [[ "${stage_status}" -eq 0 ]]; then
        if build_gcc; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
                "${PERF_RUN_ID}" \
                "${GCC_VERSION_STRING}" \
                --source-url="${GCC_SOURCE_BASE}/gcc-${SOFTWARE_VERSION}/gcc-${SOFTWARE_VERSION}.tar.xz" \
                --source-sha256="${GCC_SOURCE_SHA256}" \
                --configure-flags="--enable-languages=c,c++,fortran --disable-bootstrap --disable-multilib --disable-nls" \
                --spec-iso="${SPEC_CPU2017_ISO}"; then
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
        if start_gcc_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_gcc_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_gcc_runtime; then
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

Build GCC from the official GNU release tarball and run the SPEC CPU2017
intrate workload with the private GCC installation. Results default to
results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       gcc version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  GCC_SOURCE_BASE, GCC_OFFLINE_DIR, GCC_BENCHMARK_DATA_ROOT,
  SPEC_CPU2017_ISO
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

    if configure_runtime_paths; then
        :
    else
        return $?
    fi
    if ! mkdir -p "${RESULTS_DIR}"; then
        log "ERROR: failed to create the standalone results directory: ${RESULTS_DIR}"
        return 10
    fi
    : > "${RESULTS_DIR}/results.log"
    local pipeline_status=0
    set +e
    run_gcc_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
