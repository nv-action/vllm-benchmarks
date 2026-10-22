#!/usr/bin/env bash
# sonic-cpp 测试脚本:功能判定 + 收尾。
# 判定为:提交号 + 运行上游官方 benchmark + 链接 libsonic 的 JSON 回环程序。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/sonic_cpp_test.log"
}

# 运行上游官方 benchmark(短时);bench 二进制随仓库发布,作为上游程序可用性判定。
run_upstream_benchmark() {
    [[ -x "${SONIC_BENCH}" ]] || return 1
    (
        cd "${SOURCE_DIR}"
        "${SONIC_BENCH}" \
            --benchmark_filter=Sonic \
            --benchmark_min_time=0.01s \
            --benchmark_out_format=json \
            --benchmark_out="${RESULTS_DIR}/benchmark.json"
    )
}

# 编译并运行链接 libsonic 的 JSON 解析/序列化回环程序。
run_json_roundtrip() {
    local lib program binary
    lib="$(resolve_sonic_lib)" || return 1
    program="${HARNESS_DIR}/roundtrip.cc"
    binary="${HARNESS_DIR}/roundtrip"
    mkdir -p "${HARNESS_DIR}"
    cat >"${program}" <<'CPP'
#include "sonic/sonic.h"
#include <cstdio>
#include <string>

int main() {
    const std::string json = R"({"name":"sonic-cpp","value":42,"list":[1,2,3]})";
    sonic_json::Document doc;
    doc.Parse(json);
    sonic_json::WriteBuffer buffer;
    doc.Serialize(buffer);
    const std::string once = buffer.ToString();
    if (once.empty()) {
        std::fprintf(stderr, "empty serialization\n");
        return 1;
    }
    sonic_json::Document again;
    again.Parse(once);
    sonic_json::WriteBuffer second;
    again.Serialize(second);
    if (second.ToString() != once) {
        std::fprintf(stderr, "roundtrip mismatch\n");
        return 1;
    }
    std::printf("roundtrip ok: %s\n", once.c_str());
    return 0;
}
CPP
    c++ -std=c++11 -O2 -I"${SONIC_INCLUDE}" "${program}" "${lib}" -lpthread \
        -o "${binary}" || return 1
    LD_LIBRARY_PATH="$(dirname "${lib}")${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}" "${binary}"
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
        {"name": "json-roundtrip", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    : >"${RESULTS_DIR}/sonic_cpp_test.log"
    [[ -x "${SONIC_BENCH}" ]] || {
        log "ERROR: official benchmark binary is unavailable: ${SONIC_BENCH}"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local roundtrip_status="failed" roundtrip_detail=""
    local actual

    # 1) 版本(提交号)
    actual="$(read_sonic_cpp_version "${SOURCE_DIR}")"
    if [[ -n "${actual}" && "${actual:0:${#SOFTWARE_VERSION}}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="revision ${actual}"
    else
        version_detail="checked-out revision '${actual:-unknown}', expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游官方 benchmark
    if run_upstream_benchmark >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="official CMake benchmark (bench) ran successfully"
    else
        upstream_detail="official CMake benchmark failed; see upstream_test.log"
    fi

    # 3) 内部 smoke:JSON 解析/序列化回环
    if run_json_roundtrip >"${RESULTS_DIR}/json_roundtrip.log" 2>&1; then
        roundtrip_status="passed"
        roundtrip_detail="Parse/Serialize roundtrip ok"
    else
        roundtrip_detail="JSON roundtrip failed; see json_roundtrip.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${roundtrip_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${roundtrip_status}" "${roundtrip_detail}"
    log_line "checks: version=${version_status} upstream=${upstream_status} json-roundtrip=${roundtrip_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${HARNESS_DIR}" ]] && rm -rf -- "${HARNESS_DIR}"
    log "cleaned sonic-cpp task data"
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
Run the sonic-cpp functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    sonic-cpp revision (default: ${SOFTWARE_VERSION})
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