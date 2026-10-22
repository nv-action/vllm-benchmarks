#!/usr/bin/env bash
# Build upstream RocksDB JNI and run upstream YCSB on task-private disk data.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-6.26.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
ROCKSDB_REPOSITORY="${ROCKSDB_REPOSITORY:-https://github.com/facebook/rocksdb.git}"
YCSB_REPOSITORY="${YCSB_REPOSITORY:-https://github.com/brianfrankcooper/YCSB.git}"
YCSB_REVISION="19e885f7cb780fdded0547853f7810a150554caf"
ROCKSDB_DATA_ROOT="${ROCKSDB_DATA_ROOT:-}"

STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
SOURCE_DIR=""
YCSB_SOURCE_DIR=""
YCSB_DIST_DIR=""
MAVEN_REPO_DIR=""
MAVEN_SETTINGS_FILE=""
BENCH_DATA_DIR=""

log() {
    printf '[rocksdb] %s\n' "$*"
}

with_network_proxy() {
    if [[ -n "${PERF_PROXY:-}" ]]; then
        http_proxy="${PERF_PROXY}" https_proxy="${PERF_PROXY}" "$@"
    else
        "$@"
    fi
}

initialize_runtime() {
    local runner_architecture

    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64) EXPECTED_ARCH="x86_64" ;;
        aarch64|arm64) EXPECTED_ARCH="aarch64" ;;
        *) EXPECTED_ARCH="${EXPECTED_ARCH,,}" ;;
    esac
    runner_architecture="$(uname -m)"
    if [[ "${runner_architecture}" != "${EXPECTED_ARCH}" ]]; then
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${runner_architecture}"
        return 20
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/rocksdb/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    PERF_WORK_DIR="$(realpath -m -- "${PERF_WORK_DIR}")"
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/rocksdb/* ]]; then
        log "ERROR: RocksDB work directory must be task-private under /home/runner/boostkit-perf/rocksdb"
        return 20
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${ROCKSDB_DATA_ROOT}" ]]; then
        ROCKSDB_DATA_ROOT="${PERF_WORK_DIR}/data"
    fi
    ROCKSDB_DATA_ROOT="$(realpath -m -- "${ROCKSDB_DATA_ROOT}")"
    if [[ "${ROCKSDB_DATA_ROOT}" != "${PERF_WORK_DIR}/"* ]]; then
        log "ERROR: RocksDB data directory must be inside the task-private work directory"
        return 20
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/rocksdb-source"
    YCSB_SOURCE_DIR="${PERF_WORK_DIR}/ycsb-source"
    YCSB_DIST_DIR="${PERF_WORK_DIR}/ycsb-dist"
    MAVEN_REPO_DIR="${PERF_WORK_DIR}/maven-repository"
    MAVEN_SETTINGS_FILE="${PERF_WORK_DIR}/maven-settings.xml"
    BENCH_DATA_DIR="${ROCKSDB_DATA_ROOT}"
    if [[ -s "${PERF_WORK_DIR}/java-home.txt" ]]; then
        JAVA_HOME="$(< "${PERF_WORK_DIR}/java-home.txt")"
        if [[ ! -x "${JAVA_HOME}/bin/java" ]]; then
            log "ERROR: recorded JDK 11 is unavailable: ${JAVA_HOME}"
            return 30
        fi
        export JAVA_HOME PATH="${JAVA_HOME}/bin:${PATH}"
    fi
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE ROCKSDB_BENCH_DATA_DIR="${BENCH_DATA_DIR}" TMPDIR
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

install_rocksdb_dependencies() {
    local command_name package
    local packages=()

    for command_name in git gcc g++ make nproc python3 sed mvn flex; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            git) package="git" ;;
            gcc) package="gcc" ;;
            g++) package="gcc-c++" ;;
            make) package="make" ;;
            nproc) package="coreutils" ;;
            python3) package="python3" ;;
            sed) package="sed" ;;
            mvn) package="maven" ;;
            flex) package="flex" ;;
        esac
        packages+=("${package}")
    done
    if ! command -v javac >/dev/null 2>&1 || [[ "$(javac -version 2>&1)" != javac\ 11.* ]]; then
        packages+=("java-11-openjdk-devel")
    fi
    for package in cmake gflags-devel snappy-devel zlib-devel bzip2-devel lz4-devel zstd-devel libatomic; do
        if ! rpm -q "${package}" >/dev/null 2>&1; then
            packages+=("${package}")
        fi
    done
    if [[ "${#packages[@]}" -eq 0 ]]; then
        return 0
    fi
    local dnf_options=()
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    log "installing missing RocksDB dependencies: ${packages[*]}"
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
    elif command -v sudo >/dev/null 2>&1 && sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"; then
        :
    else
        log "ERROR: cannot install required RocksDB dependencies"
        return 30
    fi
}

build_rocksdb() {
    local source_tag actual_version major minor patch java_binary java_home
    local jar_path distribution_archive

    initialize_runtime || return $?
    install_rocksdb_dependencies || return $?
    java_binary="$(command -v javac || true)"
    if [[ -n "${java_binary}" ]]; then
        java_binary="$(readlink -f "${java_binary}")"
        java_home="$(dirname "$(dirname "${java_binary}")")"
    else
        java_home=""
    fi
    if [[ ! -x "${java_home}/bin/java" || "$("${java_home}/bin/javac" -version 2>&1)" != javac\ 11.* ]]; then
        java_binary="$(rpm -ql java-11-openjdk-devel 2>/dev/null | sed -n '/\/bin\/javac$/p' | head -1 || true)"
        if [[ -z "${java_binary}" ]]; then
            log "ERROR: a complete JDK 11 is required for upstream RocksDB/YCSB"
            return 30
        fi
        java_home="$(dirname "$(dirname "${java_binary}")")"
    fi
    if [[ ! -x "${java_home}/bin/java" || "$("${java_home}/bin/javac" -version 2>&1)" != javac\ 11.* ]]; then
        log "ERROR: selected JDK is not Java 11: ${java_home}"
        return 30
    fi
    printf '%s\n' "${java_home}" > "${PERF_WORK_DIR}/java-home.txt"
    export JAVA_HOME="${java_home}"
    export PATH="${JAVA_HOME}/bin:${PATH}"
    export MAVEN_OPTS="${MAVEN_OPTS:-} -Dmaven.repo.local=${MAVEN_REPO_DIR} -Drocksdb.version=${SOFTWARE_VERSION}"
    python3 "${SCRIPT_DIR}/scripts/write_maven_settings.py" "${MAVEN_SETTINGS_FILE}" || return 30
    source_tag="v${SOFTWARE_VERSION#v}"
    rm -rf -- "${SOURCE_DIR}" "${YCSB_SOURCE_DIR}" "${YCSB_DIST_DIR}"
    log "cloning RocksDB ${source_tag} from ${ROCKSDB_REPOSITORY}"
    if ! with_network_proxy git clone --branch "${source_tag}" --depth 1 "${ROCKSDB_REPOSITORY}" "${SOURCE_DIR}"; then
        log "ERROR: failed to clone RocksDB ${source_tag}"
        return 40
    fi
    log "building unmodified RocksDB JNI with the documented rocksdbjava target"
    if ! (cd "${SOURCE_DIR}" && export PORTABLE=1 DEBUG_LEVEL=0 && \
        with_network_proxy make rocksdbjava -j"$(nproc)" DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1); then
        log "ERROR: rocksdbjava build failed"
        return 40
    fi
    major="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_MAJOR[[:space:]]+([0-9]+).*/\1/p' "${SOURCE_DIR}/include/rocksdb/version.h")"
    minor="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_MINOR[[:space:]]+([0-9]+).*/\1/p' "${SOURCE_DIR}/include/rocksdb/version.h")"
    patch="$(sed -nE 's/^#define[[:space:]]+ROCKSDB_PATCH[[:space:]]+([0-9]+).*/\1/p' "${SOURCE_DIR}/include/rocksdb/version.h")"
    actual_version="${major}.${minor}.${patch}"
    if [[ "${actual_version}" != "${SOFTWARE_VERSION#v}" ]]; then
        log "ERROR: built RocksDB is ${actual_version}, expected ${SOFTWARE_VERSION}"
        return 40
    fi
    jar_path="${SOURCE_DIR}/java/target/rocksdbjni-${actual_version}-linux64.jar"
    if [[ ! -s "${jar_path}" ]]; then
        log "ERROR: architecture-specific RocksDB JNI JAR is missing: ${jar_path}"
        return 40
    fi
    log "installing the locally built JNI JAR into this task's private Maven repository"
    if ! mvn -B -s "${MAVEN_SETTINGS_FILE}" install:install-file \
        -Dfile="${jar_path}" -DgroupId=org.rocksdb -DartifactId=rocksdbjni \
        -Dversion="${actual_version}" -Dpackaging=jar -DgeneratePom=true; then
        log "ERROR: failed to install locally built RocksDB JNI JAR"
        return 40
    fi
    log "cloning unmodified YCSB revision ${YCSB_REVISION}"
    if ! with_network_proxy git clone --filter=blob:none "${YCSB_REPOSITORY}" "${YCSB_SOURCE_DIR}" || \
       ! with_network_proxy git -C "${YCSB_SOURCE_DIR}" checkout --detach "${YCSB_REVISION}"; then
        log "ERROR: failed to check out the documented YCSB revision"
        return 40
    fi
    if [[ "$(git -C "${YCSB_SOURCE_DIR}" rev-parse HEAD)" != "${YCSB_REVISION}" ]]; then
        log "ERROR: YCSB revision verification failed"
        return 40
    fi
    log "building the original YCSB RocksDB binding against the locally built JNI JAR"
    if ! (cd "${YCSB_SOURCE_DIR}" && mvn -B -s "${MAVEN_SETTINGS_FILE}" \
        -pl site.ycsb:rocksdb-binding -am clean package -DskipTests \
        -Drocksdb.version="${actual_version}"); then
        log "ERROR: YCSB RocksDB binding build failed"
        return 40
    fi
    distribution_archive="${YCSB_SOURCE_DIR}/rocksdb/target/ycsb-rocksdb-binding-0.18.0-SNAPSHOT.tar.gz"
    if [[ ! -s "${distribution_archive}" ]]; then
        log "ERROR: YCSB RocksDB binding distribution is missing"
        return 40
    fi
    mkdir -p "${YCSB_DIST_DIR}"
    if ! tar -xzf "${distribution_archive}" -C "${YCSB_DIST_DIR}" --strip-components=1; then
        log "ERROR: failed to extract YCSB RocksDB binding distribution"
        return 40
    fi
    if [[ ! -s "${YCSB_DIST_DIR}/bin/ycsb" || ! -s "${YCSB_DIST_DIR}/lib/rocksdbjni-${actual_version}.jar" ]]; then
        log "ERROR: YCSB distribution does not contain the expected original launcher and JNI JAR"
        return 40
    fi
    if ! cmp -s "${jar_path}" "${YCSB_DIST_DIR}/lib/rocksdbjni-${actual_version}.jar"; then
        log "ERROR: YCSB distribution JNI JAR differs from the locally built RocksDB JAR"
        return 40
    fi
    printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
}

prepare_rocksdb_benchmark_data() {
    initialize_runtime || return $?
    if [[ ! -s "${YCSB_DIST_DIR}/bin/ycsb" ]]; then
        log "ERROR: YCSB RocksDB binding is not built"
        return 40
    fi
    if [[ -e "${BENCH_DATA_DIR}" ]]; then
        log "ERROR: benchmark data directory already exists: ${BENCH_DATA_DIR}"
        return 20
    fi
    mkdir -p "${BENCH_DATA_DIR}"
    log "prepared task-private benchmark data directory: ${BENCH_DATA_DIR}"
}

run_rocksdb_benchmarks() {
    initialize_runtime || return $?
    if [[ ! -s "${YCSB_DIST_DIR}/bin/ycsb" || ! -d "${BENCH_DATA_DIR}" ]]; then
        log "ERROR: build or benchmark data preparation is incomplete"
        return 50
    fi
    python3 "${SCRIPT_DIR}/scripts/run_ycsb.py" \
        "${YCSB_DIST_DIR}" "${RESULTS_DIR}/results.json" \
        "${RESULTS_DIR}/ycsb_raw.log" || return 50
}

cleanup_rocksdb_benchmark_data() {
    initialize_runtime || return $?
    if [[ ! -e "${BENCH_DATA_DIR}" ]]; then
        log "benchmark data directory is already absent"
        return 0
    fi
    if [[ "${BENCH_DATA_DIR}" != "${ROCKSDB_DATA_ROOT}" ]]; then
        log "ERROR: refusing to remove unexpected data directory: ${BENCH_DATA_DIR}"
        return 70
    fi
    rm -rf -- "${BENCH_DATA_DIR}"
    log "removed task-private benchmark data directory: ${BENCH_DATA_DIR}"
}

cleanup_standalone_work_directory() {
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 || "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        return 0
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/rocksdb/local-* ]]; then
        log "ERROR: refusing to remove unexpected standalone work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}"
    log "removed standalone build directory: ${PERF_WORK_DIR}"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

run_rocksdb_standalone() {
    local status=0 cleanup_status="passed" failed_stage=""

    initialize_runtime || return $?
    trap 'cleanup_rocksdb_benchmark_data >/dev/null 2>&1 || true; cleanup_standalone_work_directory >/dev/null 2>&1 || true' EXIT
    standalone_runtime system "${RESULTS_DIR}/system_info.json" || { status=$?; failed_stage="prepare"; }
    if [[ "${status}" -eq 0 ]]; then
        standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json" || { status=$?; failed_stage="prepare"; }
    fi
    if [[ "${status}" -eq 0 ]]; then
        build_rocksdb || { status=$?; failed_stage="build"; }
    fi
    if [[ "${status}" -eq 0 ]]; then
        standalone_runtime build-info "${RESULTS_DIR}/build_info.json" "${SOFTWARE_VERSION}" \
            "${PERF_ACTUAL_VERSION_FILE}" "${EXPECTED_ARCH}" "${PERF_RUN_ID}" || { status=$?; failed_stage="build"; }
    fi
    if [[ "${status}" -eq 0 ]]; then
        prepare_rocksdb_benchmark_data || { status=$?; failed_stage="start"; }
    fi
    if [[ "${status}" -eq 0 ]]; then
        run_rocksdb_benchmarks || { status=$?; failed_stage="test"; }
    fi
    cleanup_rocksdb_benchmark_data || cleanup_status="failed"
    standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json" || cleanup_status="failed"
    if [[ "${status}" -eq 0 ]]; then
        standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
            "${PERF_RUN_ID}" passed "${cleanup_status}" "" || status=$?
    else
        standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
            "${PERF_RUN_ID}" failed "${cleanup_status}" "${failed_stage}" || true
    fi
    cleanup_standalone_work_directory || status=$?
    trap - EXIT
    return "${status}"
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version) SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir) RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help)
                printf 'Usage: %s [--version VERSION] [--results-dir DIRECTORY] [--keep-workdir]\n' "$(basename "$0")"
                return 0
                ;;
            *) log "ERROR: unsupported option: $1"; return 10 ;;
        esac
    done
    run_rocksdb_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
