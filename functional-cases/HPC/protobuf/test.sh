#!/usr/bin/env bash
# Protobuf 测试脚本:功能判定 + 收尾。
# 判定为:protoc 版本 + protoc 编译/回解上游 descriptor.proto + 生成 C++ 代码的序列化回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/protobuf_test.log"
}

# 用 protoc 编译上游 descriptor.proto,再用同一 protoc 解码回文本,校验前后端自洽。
run_protoc_roundtrip() {
    local inc="${PROTOBUF_INC}"
    local proto="${inc}/google/protobuf/descriptor.proto"
    [[ -f "${proto}" ]] || return 1
    "${PROTOC_BIN}" -I "${inc}" \
        --descriptor_set_out="${RESULTS_DIR}/descriptor_set.pb" "${proto}" || return 1
    "${PROTOC_BIN}" -I "${inc}" --decode=google.protobuf.FileDescriptorSet "${proto}" \
        <"${RESULTS_DIR}/descriptor_set.pb" >"${RESULTS_DIR}/descriptor_set.txt" || return 1
    [[ -s "${RESULTS_DIR}/descriptor_set.pb" ]] || return 1
    # --decode 输出为缩进文本,字段行形如 '  name: "..."',因此不能锚定行首。
    grep -q '^[[:space:]]*name:' "${RESULTS_DIR}/descriptor_set.txt" || return 1
}

# 生成 C++ 代码 + 编译链接 libprotobuf 的最小 roundtrip。
run_cpp_roundtrip() {
    local dir="${HARNESS_DIR}/cpp_roundtrip"
    local build="${dir}/build"
    mkdir -p "${dir}"
    cat >"${dir}/message.proto" <<'PROTO'
syntax = "proto3";
package tutorial;
message Sample {
  int32 id = 1;
  string name = 2;
}
PROTO
    cat >"${dir}/main.cpp" <<'CPP'
#include "message.pb.h"
#include <string>

int main() {
    tutorial::Sample sample;
    sample.set_id(7);
    sample.set_name("protobuf");
    std::string buffer;
    if (!sample.SerializeToString(&buffer)) {
        return 1;
    }
    tutorial::Sample parsed;
    if (!parsed.ParseFromString(buffer)) {
        return 1;
    }
    if (parsed.id() != 7 || parsed.name() != "protobuf") {
        return 1;
    }
    return 0;
}
CPP
    cat >"${dir}/CMakeLists.txt" <<'CMAKE'
cmake_minimum_required(VERSION 3.16)
project(pb_roundtrip CXX)
set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
# libprotobuf 依赖大量 absl 符号,只有 protobuf 自带的 CONFIG 包导出的
# protobuf::libprotobuf 才携带这些链接依赖;FindProtobuf 模块模式不会。
find_package(protobuf CONFIG REQUIRED)
add_executable(pb_roundtrip main.cpp message.proto)
protobuf_generate(TARGET pb_roundtrip LANGUAGE cpp PROTOC_EXE "${PROTOC_EXE}")
target_include_directories(pb_roundtrip PRIVATE ${CMAKE_CURRENT_BINARY_DIR})
target_link_libraries(pb_roundtrip PRIVATE protobuf::libprotobuf)
CMAKE
    local -a prefix_args=()
    [[ -z "${PROTOBUF_CMAKE_PREFIX}" ]] || prefix_args+=("-DCMAKE_PREFIX_PATH=${PROTOBUF_CMAKE_PREFIX}")
    cmake -S "${dir}" -B "${build}" -DCMAKE_BUILD_TYPE=Release \
        -DPROTOC_EXE="${PROTOC_BIN}" "${prefix_args[@]}" || return 1
    cmake --build "${build}" -j"$(nproc)" || return 1
    "${build}/pb_roundtrip"
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" u_status="$4" u_detail="$5" c_status="$6" c_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_U_STATUS="${u_status}" FUNC_U_DETAIL="${u_detail}" \
        FUNC_C_STATUS="${c_status}" FUNC_C_DETAIL="${c_detail}" \
        python3 - "${RESULTS_DIR}/functional_result.json" <<'PY'
import json, os, sys

out = sys.argv[1]
env = os.environ.get
payload = {
    "software": env("SOFTWARE_NAME"),
    "category": env("CATEGORY"),
    "version": env("SOFTWARE_VERSION"),
    "os": env("FUNC_OS"),
    "mode": env("FUNC_MODE"),
    "hardware": env("FUNC_HARDWARE") or None,
    "architecture": env("EXPECTED_ARCH"),
    "run_id": env("PERF_RUN_ID"),
    "status": env("FUNC_OVERALL"),
    "checks": [
        {"name": "version", "status": env("FUNC_V_STATUS"), "detail": env("FUNC_V_DETAIL")},
        {"name": "upstream-test", "status": env("FUNC_U_STATUS"), "detail": env("FUNC_U_DETAIL")},
        {"name": "cpp-roundtrip", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
    ],
}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, out)
PY
}

func_check() {
    initialize_runtime || return $?
    : >"${RESULTS_DIR}/protobuf_test.log"
    [[ -x "${PROTOC_BIN}" ]] || {
        log "ERROR: protoc is unavailable"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local roundtrip_status="failed" roundtrip_detail=""
    local actual

    # 1) 版本
    actual="$(read_protoc_version "${PROTOC_BIN}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="libprotoc ${actual}"
    else
        version_detail="protoc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游 protoc 编译/回解
    if run_protoc_roundtrip >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="protoc compiled and re-decoded descriptor.proto (include root: ${PROTOBUF_INC})"
    else
        upstream_detail="protoc descriptor roundtrip failed; see upstream_test.log"
    fi

    # 3) C++ 生成代码序列化回环
    if run_cpp_roundtrip >"${RESULTS_DIR}/cpp_roundtrip.log" 2>&1; then
        roundtrip_status="passed"
        roundtrip_detail="generated C++ message Serialize/Parse roundtrip ok"
    else
        roundtrip_detail="C++ serialization roundtrip failed; see cpp_roundtrip.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${roundtrip_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${roundtrip_status}" "${roundtrip_detail}"
    log_line "checks: version=${version_status} upstream=${upstream_status} cpp-roundtrip=${roundtrip_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${HARNESS_DIR}" ]] && rm -rf -- "${HARNESS_DIR}"
    log "cleaned protobuf task data"
}

# ---------- 独立运行入口 ----------

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    [[ "${STANDALONE_OWNS_WORK_DIR}" -eq 1 ]] || return 0
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-func/${SOFTWARE_NAME}/local-* ]] || {
        log "ERROR: refusing to remove unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    [[ -d "${PERF_WORK_DIR}" ]] && rm -rf -- "${PERF_WORK_DIR}"
    log "removed standalone work directory: ${PERF_WORK_DIR}"
}

emergency_cleanup() {
    set +e
    func_cleanup
    cleanup_standalone_workdir
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Run the protobuf functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    protobuf version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --mode MODE          prebuilt | build (default: ${FUNC_MODE})
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_test_standalone() {
    local stage_status=0 failed_stage="" finalize_status=0

    configure_runtime_paths || return $?
    trap emergency_cleanup EXIT
    initialize_runtime || return $?

    func_check || {
        stage_status=$?
        failed_stage="check"
    }
    func_cleanup || true
    cleanup_standalone_workdir || finalize_status=70
    trap - EXIT

    log "standalone results: ${RESULTS_DIR}"
    if [[ "${stage_status}" -ne 0 ]]; then
        log "standalone failed at stage: ${failed_stage}"
        return "${stage_status}"
    fi
    return "${finalize_status}"
}

main() {
    parse_common_args "$@"
    case "$?" in
        0) ;;
        64)
            usage
            return 0
            ;;
        *) return 10 ;;
    esac
    mkdir -p "${RESULTS_DIR:-${SCRIPT_DIR}/results}" || return $?
    run_test_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi