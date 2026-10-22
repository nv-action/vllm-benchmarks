#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-2026.08.17.00}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
FOLLY_SOURCE_URL="${FOLLY_SOURCE_URL:-https://github.com/facebook/folly.git}"
# fast_float is a REQUIRED folly dependency that most distributions do not
# package; when it is missing, the official fast_float release below is
# fetched into the private work directory (never installed system-wide).
FAST_FLOAT_SOURCE_URL="${FAST_FLOAT_SOURCE_URL:-https://github.com/fastfloat/fast_float.git}"
FAST_FLOAT_VERSION="v8.2.10"

SOURCE_DIR=""
BUILD_DIR=""
BENCH_JSON_DIR=""
FAST_FLOAT_DIR=""
MANIFEST_FILE=""
COMPILER_BINARY=""
COMPILER_VERSION_STRING=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log_message() { printf '[folly] %s\n' "$*"; }

normalize_architecture() {
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
        log_message "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/folly/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    SOURCE_DIR="${PERF_WORK_DIR}/folly-source"
    BUILD_DIR="${PERF_WORK_DIR}/folly-build"
    BENCH_JSON_DIR="${RESULTS_DIR}/folly_benchmarks"
    BENCH_STDOUT_DIR="${RESULTS_DIR}/benchmark_stdout"
    FAST_FLOAT_DIR="${PERF_WORK_DIR}/fast_float"
    MANIFEST_FILE="${PERF_WORK_DIR}/benchmark_manifest.json"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}"
}

require_commands() {
    local required package
    local packages=() dnf_options=()
    for required in git cmake make g++ python3 curl tar sed tee nproc; do
        command -v "${required}" >/dev/null 2>&1 && continue
        case "${required}" in
            g++) package="gcc-c++" ;;
            tee) package="coreutils" ;;
            nproc) package="coreutils" ;;
            *) package="${required}" ;;
        esac
        packages+=("${package}")
    done
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log_message "ERROR: dnf is required to install Folly build dependencies"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log_message "installing missing Folly build packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    for required in git cmake make g++ python3 curl tar sed tee nproc; do
        command -v "${required}" >/dev/null 2>&1 || {
            log_message "ERROR: required command remains unavailable: ${required}"
            return 30
        }
    done
}

check_system_dependencies() {
    local check header package
    local packages=() dnf_options=()
    local checks=(
        "event2/event.h:libevent-devel"
        "openssl/ssl.h:openssl-devel"
        "fmt/format.h:fmt-devel"
        "glog/logging.h:glog-devel"
        "gtest/gtest.h:gtest-devel"
        "gmock/gmock.h:gmock-devel"
    )
    if ! printf '%s\n' \
        '#include <boost/version.hpp>' \
        '#if BOOST_VERSION < 106900' \
        '#error boost too old' \
        '#endif' \
        'int main(){return 0;}' \
        | g++ -x c++ -fsyntax-only - 2>/dev/null; then
        packages+=(boost-devel)
    fi
    for check in "${checks[@]}"; do
        header="${check%%:*}"
        package="${check#*:}"
        if ! printf '#include <%s>\nint main(){return 0;}\n' "${header}" \
            | g++ -x c++ -fsyntax-only - 2>/dev/null; then
            packages+=("${package}")
        fi
    done
    if ((${#packages[@]})); then
        command -v dnf >/dev/null 2>&1 || {
            log_message "ERROR: dnf is required to install Folly development headers"
            return 30
        }
        [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
        log_message "installing missing Folly development packages: ${packages[*]}"
        if [[ "$(id -u)" -eq 0 ]]; then
            dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        else
            command -v sudo >/dev/null 2>&1 || return 30
            sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}" || return 30
        fi
    fi
    printf '%s\n' '#include <boost/version.hpp>' '#if BOOST_VERSION < 106900' \
        '#error boost too old' '#endif' 'int main(){return 0;}' \
        | g++ -x c++ -fsyntax-only - 2>/dev/null || {
        log_message "ERROR: Boost >= 1.69 development headers remain unavailable"
        return 30
    }
    for check in "${checks[@]}"; do
        header="${check%%:*}"
        printf '#include <%s>\nint main(){return 0;}\n' "${header}" \
            | g++ -x c++ -fsyntax-only - 2>/dev/null || {
            log_message "ERROR: development header remains unavailable: ${header}"
            return 30
        }
    done
}

check_architecture() {
    local actual expected
    actual="$(normalize_architecture "$(uname -m)")"
    expected="$(normalize_architecture "${EXPECTED_ARCH}")"
    [[ "${actual}" == "${expected}" ]] || {
        log_message "ERROR: expected architecture ${expected}, runner is ${actual}"
        return 20
    }
}

prepare_compiler() {
    # The official CMake build uses the default g++ toolchain; record the
    # compiler actually used by the build.
    COMPILER_BINARY="g++"
    COMPILER_VERSION_STRING="$("${COMPILER_BINARY}" --version | head -n 1)"
    log_message "using compiler: ${COMPILER_BINARY} (${COMPILER_VERSION_STRING})"
}

prepare_folly_source() {
    [[ ! -e "${SOURCE_DIR}" ]] || {
        log_message "ERROR: source directory already exists: ${SOURCE_DIR}"
        return 30
    }
    export GIT_TERMINAL_PROMPT=0
    log_message "cloning folly v${SOFTWARE_VERSION} from ${FOLLY_SOURCE_URL}"
    git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${FOLLY_SOURCE_URL}" "${SOURCE_DIR}" || {
        log_message "ERROR: failed to clone folly v${SOFTWARE_VERSION}"
        return 30
    }
}

prepare_fast_float() {
    # fast_float is REQUIRED (CMake/FindFastFloat.cmake) but rarely packaged.
    if printf '#include <fast_float/fast_float.h>\nint main(){return 0;}\n' \
        | g++ -x c++ -fsyntax-only - 2>/dev/null; then
        log_message "using system fast_float"
        return 0
    fi
    log_message "system fast_float missing; fetching official fast_float ${FAST_FLOAT_VERSION} into the work area"
    git clone --branch "${FAST_FLOAT_VERSION}" --depth 1 \
        "${FAST_FLOAT_SOURCE_URL}" "${FAST_FLOAT_DIR}" || {
        log_message "ERROR: failed to fetch fast_float ${FAST_FLOAT_VERSION}"
        return 30
    }
    [[ -f "${FAST_FLOAT_DIR}/include/fast_float/fast_float.h" ]] || {
        log_message "ERROR: fast_float headers were not fetched"
        return 30
    }
}

generate_benchmark_manifest() {
    # Extract the declared official BENCHMARK target list (directory + CMake
    # target name) from CMakeLists.txt. Optional dependencies can cause CMake
    # to omit individual targets; filter_configured_benchmark_targets narrows
    # this list to the targets actually generated for the runner.
    python3 - "${SOURCE_DIR}" "${MANIFEST_FILE}" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

source_dir, manifest_path = Path(sys.argv[1]), Path(sys.argv[2])
targets = []
current_dir = ""
for line in (source_dir / "CMakeLists.txt").read_text(encoding="utf-8").splitlines():
    stripped = line.split("#", 1)[0].strip()
    directory_match = re.match(r"DIRECTORY\s+(\S+)", stripped)
    if directory_match:
        current_dir = directory_match.group(1).rstrip("/")
        continue
    benchmark_match = re.match(r"BENCHMARK\s+([A-Za-z0-9_]+)", stripped)
    if benchmark_match:
        targets.append({"dir": current_dir, "target": benchmark_match.group(1)})
if not targets:
    raise SystemExit("no official BENCHMARK targets found in CMakeLists.txt")
# Keep a small, representative official subset during adapter iteration.
# Restore the full target list here when the complete Folly suite is ready to
# run again. These targets cover containers, concurrency, futures, hashing,
# I/O, and strings without the multi-minute or currently crashing benchmarks.
representative_targets = {
    "container_bit_iterator_bench",
    "concurrency_concurrent_hash_map_bench",
    "futures_benchmark",
    "hash_checksum_benchmark",
    "io_iobuf_benchmark",
    "string_benchmark",
}
skipped = [
    entry["target"]
    for entry in targets
    if entry["target"] not in representative_targets
]
targets = [
    entry for entry in targets if entry["target"] in representative_targets
]
manifest_path.write_text(
    json.dumps(targets, indent=2) + "\n", encoding="utf-8"
)
print(f"recorded {len(targets)} representative official BENCHMARK targets")
if skipped:
    print("skipped non-representative benchmark targets: " + ", ".join(skipped))
PYEOF
}

filter_configured_benchmark_targets() {
    local target_help_file="${PERF_WORK_DIR}/cmake-target-help.txt"
    cmake --build "${BUILD_DIR}" --target help > "${target_help_file}" || {
        log_message "ERROR: unable to list generated CMake targets"
        return 40
    }
    python3 - "${MANIFEST_FILE}" "${target_help_file}" <<'PYEOF'
import json
import re
import sys
from pathlib import Path

manifest_path, target_help_path = map(Path, sys.argv[1:])
available = {
    match.group(1)
    for match in re.finditer(r"^\.\.\. ([^ ]+)$", target_help_path.read_text(encoding="utf-8"), re.MULTILINE)
}
declared = json.loads(manifest_path.read_text(encoding="utf-8"))
configured = [entry for entry in declared if entry["target"] in available]
skipped = [entry["target"] for entry in declared if entry["target"] not in available]
if not configured:
    raise SystemExit("CMake generated no official Folly benchmark targets")
manifest_path.write_text(json.dumps(configured, indent=2) + "\n", encoding="utf-8")
print(f"selected {len(configured)} generated official BENCHMARK targets")
if skipped:
    print("skipped unavailable optional benchmark targets: " + ", ".join(skipped))
PYEOF
}

report_actual_version() {
    # folly has no --version binary; the cloned source tag is the
    # authoritative version evidence.
    local actual_version
    actual_version="$(git -C "${SOURCE_DIR}" describe --tags --exact-match 2>/dev/null || true)"
    [[ "${actual_version}" == "v${SOFTWARE_VERSION}" ]] || {
        log_message "ERROR: cloned source tag '${actual_version}' does not match v${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual_version#v}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
}

repair_incomplete_upstream_cmake() {
    # This Folly tag declares functional_partial_test, but omits its sole
    # source file (folly/functional/test/PartialTest.cpp). BUILD_BENCHMARKS
    # makes Folly register test targets too, so remove that incomplete,
    # non-benchmark declaration from this task-private checkout.
    #
    # Its generated CMake file also omits the aarch64 memcpy and memset
    # selectors from their implementation libraries, despite the official
    # Buck definition including them on Linux aarch64. The selectors define
    # __folly_memcpy and __folly_memset used by their benchmarks.
    local missing_source="${SOURCE_DIR}/folly/functional/test/PartialTest.cpp"
    local cmake_file="${SOURCE_DIR}/CMakeLists.txt"
    local folly_cmake_file="${SOURCE_DIR}/folly/CMakeLists.txt"
    python3 - "${cmake_file}" "${folly_cmake_file}" <<'PYEOF'
import sys
from pathlib import Path

cmake_file = Path(sys.argv[1])
folly_cmake_file = Path(sys.argv[2])
contents = cmake_file.read_text(encoding="utf-8")
test_entry = "      TEST functional_partial_test SOURCES PartialTest.cpp\n"
if contents.count(test_entry) != 1:
    raise SystemExit("cannot locate the unique incomplete functional_partial_test declaration")
folly_contents = folly_cmake_file.read_text(encoding="utf-8")
memcpy_entry = "  SRCS\n    FollyMemcpy.cpp\n)\n\nfolly_add_library(\n  NAME memcpy-use"
if folly_contents.count(memcpy_entry) != 1:
    raise SystemExit("cannot locate the unique memcpy-impl declaration")
memcpy_replacement = (
    "  SRCS\n"
    "    FollyMemcpy.cpp\n"
    "    $<$<BOOL:${IS_AARCH64_ARCH}>:memcpy_select_aarch64.cpp>\n"
    ")\n\nfolly_add_library(\n"
    "  NAME memcpy-use"
)
memset_entry = "  SRCS\n    FollyMemset.cpp\n)\n\nfolly_add_library(\n  NAME memset-use"
if folly_contents.count(memset_entry) != 1:
    raise SystemExit("cannot locate the unique memset-impl declaration")
memset_replacement = (
    "  SRCS\n"
    "    FollyMemset.cpp\n"
    "    $<$<BOOL:${IS_AARCH64_ARCH}>:memset_select_aarch64.cpp>\n"
    ")\n\nfolly_add_library(\n"
    "  NAME memset-use"
)
contents = contents.replace(test_entry, "")
folly_contents = (
    folly_contents.replace(memcpy_entry, memcpy_replacement)
    .replace(memset_entry, memset_replacement)
)
cmake_file.write_text(contents, encoding="utf-8")
folly_cmake_file.write_text(folly_contents, encoding="utf-8")
PYEOF
    if [[ ! -f "${missing_source}" ]]; then
        log_message "upstream tag omits PartialTest.cpp; excluded its incomplete test target"
    fi
    log_message "restored the official aarch64 memcpy and memset selectors in the private CMake build"
}

benchmark_target_list() {
    python3 - "${MANIFEST_FILE}" <<'PYEOF'
import json
import sys

for entry in json.load(open(sys.argv[1], encoding="utf-8")):
    print(entry["target"])
PYEOF
}

build_folly() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_commands || return $?
    [[ ! -e "${SOURCE_DIR}" && ! -e "${BUILD_DIR}" && ! -e "${FAST_FLOAT_DIR}" ]] || {
        log_message "ERROR: build directories are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    check_system_dependencies || return $?
    prepare_compiler || return $?
    prepare_folly_source || return $?
    prepare_fast_float || return $?
    report_actual_version || return $?
    repair_incomplete_upstream_cmake || return $?
    generate_benchmark_manifest || return $?

    local cmake_fast_float_args=()
    if [[ -d "${FAST_FLOAT_DIR}/include" ]]; then
        cmake_fast_float_args=(-DFASTFLOAT_INCLUDE_DIR="${FAST_FLOAT_DIR}/include")
    fi

    # Folly enables CMake's GoogleTest source discovery when benchmarks are
    # enabled. This release references a test source absent from its tag;
    # discovery is not required to build or run the official benchmarks.
    log_message "configuring official folly benchmarks without GoogleTest source discovery"
    cmake -S "${SOURCE_DIR}" -B "${BUILD_DIR}" \
        -DCMAKE_BUILD_TYPE=Release \
        -DBUILD_BENCHMARKS=ON \
        -DUSE_CMAKE_GOOGLE_TEST_INTEGRATION=OFF \
        "${cmake_fast_float_args[@]}" || {
        log_message "ERROR: cmake configure of folly failed"
        return 40
    }
    filter_configured_benchmark_targets || return $?

    local target target_count build_args=()
    target_count="$(benchmark_target_list | wc -l)"
    while IFS= read -r target; do
        build_args+=(--target "${target}")
    done < <(benchmark_target_list)
    log_message "building ${target_count} official benchmark targets"
    cmake --build "${BUILD_DIR}" -j "$(nproc)" "${build_args[@]}" || {
        log_message "ERROR: cmake build of the official benchmark targets failed"
        return 40
    }
    local missing=0 binary
    while IFS= read -r target; do
        binary="${BUILD_DIR}/${target}"
        if [[ ! -x "${binary}" ]]; then
            log_message "ERROR: official benchmark binary was not produced: ${binary}"
            missing=1
        fi
    done < <(benchmark_target_list)
    [[ "${missing}" -eq 0 ]] || return 40
    log_message "folly ${SOFTWARE_VERSION} official benchmark artifacts are ready"
}

start_folly_runtime() {
    initialize_runtime || return $?
    [[ -f "${MANIFEST_FILE}" ]] || {
        log_message "ERROR: benchmark manifest is unavailable: ${MANIFEST_FILE}"
        return 40
    }
    local target binary
    while IFS= read -r target; do
        binary="${BUILD_DIR}/${target}"
        [[ -x "${binary}" ]] || {
            log_message "ERROR: official benchmark binary is unavailable: ${binary}"
            return 40
        }
    done < <(benchmark_target_list)
    log_message "folly official benchmark runtime is ready"
}

run_folly_benchmarks() {
    initialize_runtime || return $?
    [[ -f "${MANIFEST_FILE}" ]] || {
        log_message "ERROR: benchmark manifest is unavailable: ${MANIFEST_FILE}"
        return 40
    }
    mkdir -p "${BENCH_JSON_DIR}" "${BENCH_STDOUT_DIR}"
    local target binary output_file stdout_file started_at elapsed_seconds
    while IFS= read -r target; do
        binary="${BUILD_DIR}/${target}"
        output_file="${BENCH_JSON_DIR}/${target}.json"
        stdout_file="${BENCH_STDOUT_DIR}/${target}.log"
        [[ -x "${binary}" ]] || {
            log_message "ERROR: official benchmark binary is unavailable: ${binary}"
            return 40
        }
        log_message "running official benchmark target: ${target}"
        started_at="$(date +%s)"
        (
            # The official add_test entries run from the repository root.
            cd "${SOURCE_DIR}"
            case "${target}" in
                concurrency_concurrent_hash_map_bench|io_async_request_context_benchmark)
                    # These official benchmarks print their own tables and do
                    # not define Folly's --bm_json_verbose gflag.
                    "${binary}" > "${stdout_file}" 2>&1
                    ;;
                *)
                    "${binary}" "--bm_json_verbose=${output_file}" > "${stdout_file}" 2>&1
                    ;;
            esac
        ) || {
            log_message "ERROR: official benchmark target failed: ${target}"
            return 50
        }
        elapsed_seconds="$(( $(date +%s) - started_at ))"
        log_message "completed official benchmark target: ${target} (${elapsed_seconds}s)"
    done < <(benchmark_target_list)

    export SOFTWARE_VERSION EXPECTED_ARCH
    python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
        "${MANIFEST_FILE}" \
        "${BENCH_JSON_DIR}" \
        "${BENCH_STDOUT_DIR}" \
        "${RESULTS_DIR}/benchmark_folly.json" || {
        log_message "ERROR: failed to normalize official folly benchmark results"
        return 50
    }
    log_message "folly benchmark results written to benchmark_folly.json"
}

stop_folly_runtime() {
    log_message "folly benchmark has no background service to stop"
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
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/folly/local-* && \
       "${PERF_WORK_DIR}" != "/home/runner/boostkit-perf/folly" ]] || {
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
        stop_folly_runtime
    fi
    if [[ "${STANDALONE_CLEANUP_DONE}" -ne 1 ]]; then
        cleanup_standalone_workdir
    fi
}

run_folly_standalone() {
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
        if build_folly; then
            if standalone_runtime build-info \
                "${RESULTS_DIR}/build_info.json" \
                "${SOFTWARE_VERSION}" \
                "${PERF_ACTUAL_VERSION_FILE}" \
                "$(normalize_architecture "${EXPECTED_ARCH}")" \
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
        if start_folly_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_folly_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi

    if ! stop_folly_runtime; then
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

Build and run Folly's official CMake BENCHMARK targets as a standalone
performance evaluation. Standard targets use --bm_json_verbose; targets with
their own official text tables retain and parse that output instead.
Results default to results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       folly version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help

Environment overrides:
  SOFTWARE_VERSION, EXPECTED_ARCH, RESULTS_DIR, PERF_WORK_DIR,
  FOLLY_SOURCE_URL, FAST_FLOAT_SOURCE_URL
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
    run_folly_standalone 2>&1 | tee -a "${RESULTS_DIR}/results.log"
    pipeline_status="${PIPESTATUS[0]}"
    set -e
    log_message "standalone results: ${RESULTS_DIR}"
    return "${pipeline_status}"
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
