#!/usr/bin/env bash
# openjdk (OpenJDK) 测试脚本:功能判定 + 收尾。
# 判定叠加版本校验、javac 编译 + java 运行 Hello 与 java.util.stream smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# javac 编译 + java 运行一个 Hello 程序。
run_hello_program() {
    local dir="${PERF_WORK_DIR}/smoke"
    rm -rf -- "${dir}"
    mkdir -p "${dir}"
    cat >"${dir}/Hello.java" <<'JAVA'
public class Hello {
    public static void main(String[] args) {
        System.out.println("hello-openjdk");
    }
}
JAVA
    (
        cd "${dir}"
        "${JAVAC_BIN}" -d "${dir}" "${dir}/Hello.java"
        "${JAVA_BIN}" -cp "${dir}" Hello
    ) | grep -qx hello-openjdk
}

# java 运行一个使用 java.util.stream 的程序。
run_java_smoke() {
    local dir="${PERF_WORK_DIR}/smoke"
    mkdir -p "${dir}"
    cat >"${dir}/Smoke.java" <<'JAVA'
import java.util.List;
import java.util.stream.Collectors;

public class Smoke {
    public static void main(String[] args) {
        List<Integer> values = List.of(1, 2, 3, 4);
        int sum = values.stream().mapToInt(Integer::intValue).sum();
        String joined = values.stream().map(value -> value.toString()).collect(Collectors.joining("-"));
        System.out.println("openjdk-smoke-" + sum + "-" + joined);
    }
}
JAVA
    (
        cd "${dir}"
        "${JAVAC_BIN}" -d "${dir}" "${dir}/Smoke.java"
        "${JAVA_BIN}" -cp "${dir}" Smoke
    ) | grep -qx openjdk-smoke-10-1-2-3-4
}

write_functional_result() {
    local overall="$1"
    shift
    FUNC_OVERALL="${overall}" python3 - "${RESULTS_DIR}/functional_result.json" "$@" <<'PY'
import json, os, sys

out = sys.argv[1]
items = sys.argv[2:]
env = os.environ.get
checks = [
    {"name": items[i], "status": items[i + 1], "detail": items[i + 2]}
    for i in range(0, len(items), 3)
]
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
    "checks": checks,
}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, out)
PY
}

func_check() {
    initialize_runtime || return $?
    [[ -x "${JAVA_BIN}" && -x "${JAVAC_BIN}" ]] || {
        log "ERROR: java/javac are unavailable: ${JAVA_BIN:-unset} ${JAVAC_BIN:-unset}"
        return 40
    }
    mkdir -p "${RESULTS_DIR}"
    : >"${RESULTS_DIR}/check.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual feature requested

    # 1) 版本(java -version 打到 stderr;GA/EA 只在 feature 号上可比)
    requested="$(requested_feature)"
    actual="$(java_version_of "${JAVA_BIN}")"
    feature="$(java_feature_of "${JAVA_BIN}")"
    if [[ "${feature}" == "${requested}" ]]; then
        version_status="passed"
        version_detail="java ${actual} (feature ${feature})"
    else
        version_detail="java feature ${feature:-unknown} (java ${actual:-unknown}), expected ${requested}"
    fi

    # 2) 上游测试(替代):javac 编译 + java 运行 Hello。
    if run_hello_program >>"${RESULTS_DIR}/check.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="javac compiled and java ran Hello"
    else
        upstream_detail="javac/java Hello failed; see check.log"
    fi

    # 3) 基础功能 smoke:java.util.stream。
    if run_java_smoke >>"${RESULTS_DIR}/check.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="java.util.stream program ran ok"
    else
        smoke_detail="java.util.stream smoke failed; see check.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    # 成功路径下 javac/java 不产生多余输出,check.log 会一直是空文件;
    # 框架要求该输出非空,故这里显式落一份判定摘要。
    {
        printf 'version: %s (%s)\n' "${version_status}" "${version_detail}"
        printf 'upstream-test: %s (%s)\n' "${upstream_status}" "${upstream_detail}"
        printf 'smoke: %s (%s)\n' "${smoke_status}" "${smoke_detail}"
    } >>"${RESULTS_DIR}/check.log"
    write_functional_result "${overall}" \
        "version" "${version_status}" "${version_detail}" \
        "upstream-test" "${upstream_status}" "${upstream_detail}" \
        "smoke" "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream=${upstream_status} smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${PERF_WORK_DIR}/smoke" ]] && rm -rf -- "${PERF_WORK_DIR}/smoke"
    log "cleaned openjdk task data"
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
Run the OpenJDK functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    JDK version: feature(27)或 feature_build(28_19),默认 ${SOFTWARE_VERSION}
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
