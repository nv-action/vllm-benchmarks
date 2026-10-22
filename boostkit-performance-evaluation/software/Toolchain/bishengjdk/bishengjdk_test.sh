#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-21.0.9}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
BISHENGJDK_SOURCE_BASE="${BISHENGJDK_SOURCE_BASE:-https://github.com/openeuler-mirror}"
ADOPTIUM_RELEASE_BASE="${ADOPTIUM_RELEASE_BASE:-https://github.com/adoptium/temurin21-binaries/releases/download}"
BISHENGJDK_MAVEN_MIRROR="${BISHENGJDK_MAVEN_MIRROR:-https://repo.maven.apache.org/maven2}"
BISHENGJDK_OFFLINE_DIR="${BISHENGJDK_OFFLINE_DIR:-/home/runner/software/bishengjdk}"
BISHENGJDK_BOOT_JDK_HOME="${BISHENGJDK_BOOT_JDK_HOME:-}"
JMH_BENCHMARK="org.openjdk.bench.vm.compiler.FloatingScalarVectorAbsDiff"
JMH_COUNT=1024
JMH_VERSION="1.37"

JDK_HOME=""
SRC_DIR=""
BOOT_JDK_HOME=""
BUILD_CONF=""
JDK_VERSION_STRING=""
BISHENGJDK_SOURCE_REPO=""
BISHENGJDK_SOURCE_TAG=""
BISHENGJDK_SOURCE_URL=""
BISHENGJDK_SOURCE_SHA256=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[bishengjdk] %s\n' "$*"
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
        PERF_WORK_DIR="/home/runner/boostkit-perf/bishengjdk/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    JDK_HOME="${PERF_WORK_DIR}/jdk"
    SRC_DIR="${PERF_WORK_DIR}/bishengjdk-source"
    BUILD_CONF="linux-${EXPECTED_ARCH}-server-release"
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
    local required header missing=0
    for required in curl tar sha256sum python3 awk sed grep tee make gcc g++ find; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            missing=1
        fi
    done
    for header in \
        /usr/include/alsa/asoundlib.h \
        /usr/include/fontconfig/fontconfig.h \
        /usr/include/freetype2/ft2build.h \
        /usr/include/cups/cups.h \
        /usr/include/X11/extensions/shape.h \
        /usr/include/X11/extensions/Xrender.h \
        /usr/include/X11/extensions/Xrandr.h \
        /usr/include/X11/extensions/XTest.h \
        /usr/include/X11/extensions/XInput2.h \
        /usr/include/X11/Intrinsic.h; do
        if [[ ! -f "${header}" ]]; then
            missing=1
        fi
    done
    if [[ "${missing}" -eq 0 ]]; then
        return 0
    fi
    log "installing missing BiSheng JDK build dependencies"
    local package_manager_options=()
    [[ -z "${PERF_PROXY:-}" ]] || package_manager_options+=("--setopt=proxy=${PERF_PROXY}")
    if ! command -v dnf >/dev/null 2>&1; then
        log "ERROR: dnf is required to install BiSheng JDK build dependencies"
        return 30
    fi
    local install_command=(dnf)
    if [[ "${EUID}" -ne 0 ]]; then
        if ! command -v sudo >/dev/null 2>&1; then
            log "ERROR: root privileges are required to install BiSheng JDK build dependencies"
            return 30
        fi
        install_command=(sudo -n dnf)
    fi
    if ! "${install_command[@]}" "${package_manager_options[@]}" install -y \
        curl tar gzip coreutils python3 gawk findutils sed grep make gcc gcc-c++ \
        freetype-devel fontconfig-devel alsa-lib-devel cups-devel \
        libXtst-devel libXt-devel libXrender-devel libXrandr-devel libXi-devel; then
        log "ERROR: failed to install BiSheng JDK build dependencies"
        return 30
    fi
    for required in curl tar sha256sum python3 awk sed grep tee make gcc g++ find; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required command is still missing after installation: ${required}"
            return 30
        fi
    done
    for header in \
        /usr/include/alsa/asoundlib.h \
        /usr/include/fontconfig/fontconfig.h \
        /usr/include/freetype2/ft2build.h \
        /usr/include/cups/cups.h \
        /usr/include/X11/extensions/shape.h \
        /usr/include/X11/extensions/Xrender.h \
        /usr/include/X11/extensions/Xrandr.h \
        /usr/include/X11/extensions/XTest.h \
        /usr/include/X11/extensions/XInput2.h \
        /usr/include/X11/Intrinsic.h; do
        if [[ ! -f "${header}" ]]; then
            log "ERROR: required BiSheng JDK development header is still missing: ${header}"
            return 30
        fi
    done
}

prepare_bishengjdk_source() {
    local archive_name archive_path local_archive_path top_dir

    case "${SOFTWARE_VERSION}" in
        21.0.9)
            BISHENGJDK_SOURCE_REPO="bishengjdk-21"
            BISHENGJDK_SOURCE_TAG="jdk-21.0.9-ga-b011"
            ;;
        *)
            log "ERROR: no BiSheng JDK source tag is declared for ${SOFTWARE_VERSION}"
            return 30
            ;;
    esac
    archive_name="${BISHENGJDK_SOURCE_REPO}-${BISHENGJDK_SOURCE_TAG}.tar.gz"
    local_archive_path="${BISHENGJDK_OFFLINE_DIR}/${archive_name}"
    archive_path="${PERF_WORK_DIR}/${archive_name}"
    BISHENGJDK_SOURCE_URL="${BISHENGJDK_SOURCE_BASE}/${BISHENGJDK_SOURCE_REPO}/archive/refs/tags/${BISHENGJDK_SOURCE_TAG}.tar.gz"
    if [[ -f "${local_archive_path}" ]]; then
        log "using local BiSheng JDK GA source archive ${local_archive_path}"
        if ! cp "${local_archive_path}" "${archive_path}"; then
            log "ERROR: failed to copy local BiSheng JDK source archive"
            return 30
        fi
    else
        log "downloading BiSheng JDK GA source ${BISHENGJDK_SOURCE_TAG}"
        if ! curl -fL --retry 3 --connect-timeout 30 -o "${archive_path}" "${BISHENGJDK_SOURCE_URL}"; then
            log "ERROR: failed to download BiSheng JDK GA source archive"
            return 30
        fi
    fi
    BISHENGJDK_SOURCE_SHA256="$(sha256sum "${archive_path}" | awk '{print $1}')"
    top_dir="$(tar -tzf "${archive_path}" | awk -F/ 'NR == 1 {print $1}')"
    if [[ -z "${top_dir}" || "${top_dir}" != "${BISHENGJDK_SOURCE_REPO}-"* ]]; then
        log "ERROR: BiSheng JDK source archive has an unexpected root directory: ${top_dir}"
        return 30
    fi
    if ! tar -xzf "${archive_path}" -C "${PERF_WORK_DIR}"; then
        log "ERROR: failed to extract the BiSheng JDK GA source archive"
        return 30
    fi
    if ! mv "${PERF_WORK_DIR}/${top_dir}" "${SRC_DIR}"; then
        log "ERROR: failed to place the BiSheng JDK source tree"
        return 30
    fi
    rm -f "${archive_path}"
    if [[ ! -f "${SRC_DIR}/test/micro/org/openjdk/bench/vm/compiler/FloatingScalarVectorAbsDiff.java" ]]; then
        log "ERROR: BiSheng JDK source is missing ${JMH_BENCHMARK}"
        return 30
    fi
    log "BiSheng JDK GA source is ready: ${BISHENGJDK_SOURCE_TAG} (${BISHENGJDK_SOURCE_SHA256})"
}

prepare_boot_jdk() {
    local archive_name archive_path boot_jdk_release boot_jdk_version
    local local_archive_path top_dir version

    case "${SOFTWARE_VERSION}" in
        21.0.9)
            boot_jdk_version="21.0.9"
            boot_jdk_release="jdk-21.0.9%2B10"
            ;;
        *)
            log "ERROR: no Temurin boot JDK is declared for BiSheng JDK ${SOFTWARE_VERSION}"
            return 30
            ;;
    esac
    if [[ -n "${BISHENGJDK_BOOT_JDK_HOME}" ]]; then
        BOOT_JDK_HOME="${BISHENGJDK_BOOT_JDK_HOME}"
    else
        case "${EXPECTED_ARCH}" in
            x86_64)
                archive_name="OpenJDK21U-jdk_x64_linux_hotspot_${boot_jdk_version}_10.tar.gz"
                ;;
            aarch64)
                archive_name="OpenJDK21U-jdk_aarch64_linux_hotspot_${boot_jdk_version}_10.tar.gz"
                ;;
            *)
                log "ERROR: unsupported architecture for Temurin boot JDK: ${EXPECTED_ARCH}"
                return 30
                ;;
        esac
        local_archive_path="${BISHENGJDK_OFFLINE_DIR}/${archive_name}"
        archive_path="${PERF_WORK_DIR}/${archive_name}"
        if [[ -f "${local_archive_path}" ]]; then
            log "using local Temurin boot JDK archive ${local_archive_path}"
            if ! cp "${local_archive_path}" "${archive_path}"; then
                log "ERROR: failed to copy local Temurin boot JDK archive"
                return 30
            fi
        else
            log "downloading Temurin ${boot_jdk_version} boot JDK for ${EXPECTED_ARCH}"
            if ! curl -fL --retry 3 --connect-timeout 30 -o "${archive_path}" \
                "${ADOPTIUM_RELEASE_BASE}/${boot_jdk_release}/${archive_name}"; then
                log "ERROR: failed to download the Temurin boot JDK archive"
                return 30
            fi
        fi
        top_dir="$(tar -tzf "${archive_path}" | awk -F/ 'NR == 1 {print $1}')"
        if [[ -z "${top_dir}" || "${top_dir}" != jdk-* ]]; then
            log "ERROR: Temurin boot JDK archive has an unexpected root directory: ${top_dir}"
            return 30
        fi
        if ! tar -xzf "${archive_path}" -C "${PERF_WORK_DIR}"; then
            log "ERROR: failed to extract the Temurin boot JDK archive"
            return 30
        fi
        if ! mv "${PERF_WORK_DIR}/${top_dir}" "${PERF_WORK_DIR}/boot-jdk"; then
            log "ERROR: failed to place the Temurin boot JDK"
            return 30
        fi
        rm -f "${archive_path}"
        BOOT_JDK_HOME="${PERF_WORK_DIR}/boot-jdk"
    fi
    if [[ ! -x "${BOOT_JDK_HOME}/bin/java" || ! -x "${BOOT_JDK_HOME}/bin/javac" ]]; then
        log "ERROR: boot JDK is missing bin/java or bin/javac: ${BOOT_JDK_HOME}"
        return 30
    fi
    version="$("${BOOT_JDK_HOME}/bin/java" -version 2>&1 | sed -n 's/^openjdk version "\([^"]*\)".*/\1/p' | head -n 1)"
    if [[ "${version}" != "${boot_jdk_version}" ]]; then
        log "ERROR: expected Temurin boot JDK ${boot_jdk_version}, found ${version:-unknown}"
        return 30
    fi
    log "using Temurin boot JDK ${version}: ${BOOT_JDK_HOME}"
}

build_bishengjdk_from_source() {
    local build_jdk version_line actual_version

    if prepare_boot_jdk; then
        :
    else
        return $?
    fi
    if ! (
        cd "${SRC_DIR}"
        MAVEN_MIRROR="${BISHENGJDK_MAVEN_MIRROR}" sh make/devkit/createJMHBundle.sh
    ); then
        log "ERROR: failed to prepare the official JMH bundle"
        return 30
    fi
    for jar in commons-math3-3.6.1 jopt-simple-5.0.4 jmh-core-1.37 jmh-generator-annprocess-1.37; do
        if [[ ! -s "${SRC_DIR}/build/jmh/jars/${jar}.jar" ]]; then
            log "ERROR: JMH dependency is missing: ${jar}.jar"
            return 30
        fi
    done
    log "configuring BiSheng JDK ${SOFTWARE_VERSION} GA source build with JMH"
    if ! (
        cd "${SRC_DIR}"
        bash configure \
            --with-debug-level=release \
            --with-boot-jdk="${BOOT_JDK_HOME}" \
            --with-jmh=build/jmh/jars \
            --with-jvm-variants=server \
            --prefix="${JDK_HOME}" \
            --disable-warnings-as-errors \
            --disable-precompiled-headers
    ); then
        log "ERROR: BiSheng JDK configure failed"
        return 40
    fi
    log "building BiSheng JDK images from source"
    if ! make -C "${SRC_DIR}" images; then
        log "ERROR: BiSheng JDK source build failed"
        return 40
    fi
    build_jdk="${SRC_DIR}/build/${BUILD_CONF}/images/jdk"
    if [[ ! -d "${build_jdk}" ]]; then
        log "ERROR: expected source build image is missing: ${build_jdk}"
        return 40
    fi
    if ! ln -s "${build_jdk}" "${JDK_HOME}"; then
        log "ERROR: failed to link the source-built JDK image"
        return 40
    fi
    if [[ ! -x "${JDK_HOME}/bin/java" || ! -x "${JDK_HOME}/bin/javac" ]]; then
        log "ERROR: source-built JDK is missing bin/java or bin/javac: ${JDK_HOME}"
        return 40
    fi
    version_line="$("${JDK_HOME}/bin/java" -version 2>&1 | head -n 1)"
    actual_version="$(printf '%s\n' "${version_line}" | sed -n 's/^openjdk version "\([^"]*\)".*/\1/p')"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION}-internal" ]]; then
        log "ERROR: source-built JDK reports ${actual_version:-unknown}, expected ${SOFTWARE_VERSION}-internal"
        return 40
    fi
    JDK_VERSION_STRING="${version_line}"
    # Framework uses this file to match the requested GA release.  The full
    # source-build identifier (for example, 21.0.9-internal) remains in
    # JDK_VERSION_STRING and is recorded in build_info.json.
    if ! printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"; then
        log "ERROR: failed to record the BiSheng JDK release version"
        return 40
    fi
    log "source-built BiSheng JDK is ready: ${JDK_VERSION_STRING}"
}

build_bishengjdk() {
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
    if [[ -e "${JDK_HOME}" || -e "${SRC_DIR}" || -e "${PERF_WORK_DIR}/boot-jdk" ]]; then
        log "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    fi
    if prepare_bishengjdk_source; then
        :
    else
        return $?
    fi
    if build_bishengjdk_from_source; then
        :
    else
        return $?
    fi
    log "BiSheng JDK ${SOFTWARE_VERSION} JMH runtime is built at ${PERF_WORK_DIR}"
}

start_bishengjdk_runtime() {
    if initialize_runtime; then
        :
    else
        return $?
    fi
    if [[ ! -x "${JDK_HOME}/bin/java" || \
          ! -s "${SRC_DIR}/build/jmh/jars/jmh-core-${JMH_VERSION}.jar" ]]; then
        log "ERROR: source-built BiSheng JDK or JMH bundle is missing"
        return 40
    fi
    if [[ ! -f "${SRC_DIR}/test/micro/org/openjdk/bench/vm/compiler/FloatingScalarVectorAbsDiff.java" ]]; then
        log "ERROR: JMH benchmark source is missing: ${JMH_BENCHMARK}"
        return 40
    fi
    log "BiSheng JDK JMH runtime is ready"
}

run_bishengjdk_benchmarks() {
    local result_root result_file
    local -a result_files

    if initialize_runtime; then
        :
    else
        return $?
    fi
    if [[ ! -x "${JDK_HOME}/bin/java" || \
          ! -s "${SRC_DIR}/build/jmh/jars/jmh-core-${JMH_VERSION}.jar" ]]; then
        log "ERROR: source-built BiSheng JDK or JMH bundle is missing"
        return 40
    fi
    result_root="${SRC_DIR}/build/${BUILD_CONF}/test-results"
    log "running ${JMH_BENCHMARK} with JMH ${JMH_VERSION}; count=${JMH_COUNT}, threads=1, no CPU affinity"
    if ! (
        cd "${SRC_DIR}"
        make test CONF="${BUILD_CONF}" \
            TEST="micro:${JMH_BENCHMARK}" \
            MICRO="FORK=3;WARMUP_ITER=4;WARMUP_TIME=2;ITER=4;TIME=2;RESULTS_FORMAT=json;OPTIONS=-t 1 -p count=${JMH_COUNT}"
    ) 2>&1 | tee "${RESULTS_DIR}/jmh-output.log"; then
        log "ERROR: JMH test run failed"
        return 50
    fi
    if [[ ! -s "${RESULTS_DIR}/jmh-output.log" || ! -d "${result_root}" ]]; then
        log "ERROR: JMH output or test-results directory is missing"
        return 50
    fi
    mapfile -d '' -t result_files < <(find "${result_root}" -type f -name jmh-result.json -print0)
    if [[ "${#result_files[@]}" -ne 1 || ! -s "${result_files[0]:-}" ]]; then
        log "ERROR: expected exactly one nonempty JMH JSON result under ${result_root}"
        return 50
    fi
    result_file="${result_files[0]}"
    cp "${result_file}" "${RESULTS_DIR}/jmh-result.json"
    export SOFTWARE_VERSION EXPECTED_ARCH JMH_BENCHMARK JMH_COUNT JMH_VERSION BUILD_CONF
    if ! python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${RESULTS_DIR}/jmh-result.json" \
        "${RESULTS_DIR}/benchmark_bishengjdk.json"; then
        log "ERROR: failed to normalize JMH results"
        return 50
    fi
    log "JMH results written to jmh-output.log, jmh-result.json, and benchmark_bishengjdk.json"
}

stop_bishengjdk_runtime() {
    log "BiSheng JDK benchmark has no background service to stop"
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
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/bishengjdk/local-* || \
          "${PERF_WORK_DIR}" == "/home/runner/boostkit-perf/bishengjdk" ]]; then
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
        stop_bishengjdk_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_bishengjdk_standalone() {
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
        if build_bishengjdk; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "${EXPECTED_ARCH}" \
                "${PERF_RUN_ID}" \
                "${JDK_VERSION_STRING}" \
                --source-url="${BISHENGJDK_SOURCE_URL}" \
                --source-sha256="${BISHENGJDK_SOURCE_SHA256}" \
                --source-repo="${BISHENGJDK_SOURCE_REPO}" \
                --source-tag="${BISHENGJDK_SOURCE_TAG}" \
                --boot-jdk-home="${BOOT_JDK_HOME}" \
                --jmh-version="${JMH_VERSION}" \
                --benchmark="${JMH_BENCHMARK}"; then
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
        if start_bishengjdk_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_bishengjdk_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_bishengjdk_runtime; then
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

Build the BiSheng JDK 21 GA source archive and run its official JMH
FloatingScalarVectorAbsDiff microbenchmark. Results default to
results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       BiSheng JDK version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  BISHENGJDK_SOURCE_BASE, BISHENGJDK_OFFLINE_DIR, BISHENGJDK_BOOT_JDK_HOME,
  ADOPTIUM_RELEASE_BASE, BISHENGJDK_MAVEN_MIRROR
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
    run_bishengjdk_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
