#!/usr/bin/env bash
# Folly 测试脚本:功能判定 + 收尾。
# 判定为:tag 版本 + 使用 folly 公共 API 的程序 + 最小 include/link 可用性。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/folly_test.log"
}

# 编译链接 folly 共享库;folly 的 include 根是源码目录,生成的配置头在构建目录。
compile_folly_program() {
    local program="$1" binary="$2"
    g++ -std=c++20 -O1 \
        -I"${SOURCE_DIR}" -I"${BUILD_DIR}" \
        "${program}" \
        -L"${BUILD_DIR}" -lfolly -Wl,-rpath,"${BUILD_DIR}" \
        -lpthread -ldl -o "${binary}"
}

# 上游功能判定:用 folly 公共 API(Conv/String/Range)做一次实际计算。
run_upstream_api() {
    local program="${HARNESS_DIR}/folly_api.cc"
    local binary="${HARNESS_DIR}/folly_api"
    mkdir -p "${HARNESS_DIR}"
    cat >"${program}" <<'CPP'
#include <folly/Conv.h>
#include <folly/Range.h>
#include <folly/String.h>
#include <cstdio>
#include <string>
#include <vector>

int main() {
    const std::string number = folly::to<std::string>(1234);
    if (number != "1234") {
        return 1;
    }
    std::vector<folly::StringPiece> parts;
    folly::split(',', "alpha,beta,gamma", parts);
    if (parts.size() != 3) {
        return 1;
    }
    const std::string joined = folly::join(';', parts);
    if (joined != "alpha;beta;gamma") {
        return 1;
    }
    std::printf("folly api ok: %s / %s\n", number.c_str(), joined.c_str());
    return 0;
}
CPP
    compile_folly_program "${program}" "${binary}" || return 1
    "${binary}" || return 1
}

# 最小可用性:包含一个 folly 头并链接 libfolly。
run_link_smoke() {
    local program="${HARNESS_DIR}/link_smoke.cc"
    local binary="${HARNESS_DIR}/link_smoke"
    mkdir -p "${HARNESS_DIR}"
    cat >"${program}" <<'CPP'
#include <folly/String.h>
#include <cstdio>

int main() {
    const std::string trimmed = folly::trimWhitespace("  folly  ").str();
    if (trimmed != "folly") {
        return 1;
    }
    std::printf("folly link smoke ok\n");
    return 0;
}
CPP
    compile_folly_program "${program}" "${binary}" || return 1
    "${binary}" || return 1
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
        {"name": "link-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    : >"${RESULTS_DIR}/folly_test.log"
    [[ -f "${FOLLY_LIB}" ]] || {
        log "ERROR: libfolly is unavailable: ${FOLLY_LIB}"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local link_status="failed" link_detail=""
    local actual

    # 1) 版本(tag)
    actual="$(read_folly_version "${SOURCE_DIR}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="v${actual}"
    else
        version_detail="folly v${actual:-unknown}, expected v${SOFTWARE_VERSION}"
    fi

    # 2) 上游公共 API 程序
    if run_upstream_api >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="folly public API (Conv/String/Range) program passed"
    else
        upstream_detail="folly public API program failed; see upstream_test.log"
    fi

    # 3) 最小 include/link 可用性
    if run_link_smoke >"${RESULTS_DIR}/link_smoke.log" 2>&1; then
        link_status="passed"
        link_detail="folly header include and libfolly link ok"
    else
        link_detail="folly link smoke failed; see link_smoke.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${link_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${link_status}" "${link_detail}"
    log_line "checks: version=${version_status} upstream=${upstream_status} link-smoke=${link_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${HARNESS_DIR}" ]] && rm -rf -- "${HARNESS_DIR}"
    log "cleaned folly task data"
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
Run the folly functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    folly version (default: ${SOFTWARE_VERSION})
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