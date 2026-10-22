#!/usr/bin/env bash
# Bolt 测试脚本:功能判定 + 收尾。
# 判定:版本(ref/commit)校验 + 上游 ctest(gtest)子集 + 构建产物链接冒烟。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 在构建/安装目录内查找 Bolt 原生共享库(编译链接产物的最小可用性证据)。
find_bolt_lib() {
    local root lib
    local -a roots=("${BOLT_BUILD_DIR:-}" "${BOLT_HOME:-}" "${BOLT_SOURCE_DIR:-}")
    for root in "${roots[@]}"; do
        [[ -n "${root}" && -d "${root}" ]] || continue
        local -a libs=()
        mapfile -t libs < <(find "${root}" -type f \( -name 'libbolt*.so*' -o -name 'libbolt*.a' \) 2>/dev/null | sort || true)
        # 优先共享库(ldd 证据更强);conan 默认产物是静态库,回退 libbolt*.a。
        for lib in "${libs[@]}"; do
            if [[ "${lib}" == *.so* ]]; then
                printf '%s\n' "${lib}"
                return 0
            fi
        done
        if [[ -n "${libs[0]:-}" ]]; then
            printf '%s\n' "${libs[0]}"
            return 0
        fi
    done
    return 1
}

# find_ctest_tree 已上移至 common.sh(build 模式与 prebuilt 补建逻辑共用)。

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
        {"name": "upstream-unit-test", "status": env("FUNC_U_STATUS"), "detail": env("FUNC_U_DETAIL")},
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
    : >"${RESULTS_DIR}/bolt_test.log"

    local version_status="failed" version_detail=""
    local unit_status="failed" unit_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual commit lib

    # 1) 版本:上游无 tag,版本基线为 commit。build 模式下按 commit 检出是 detached HEAD,
    #    actual = 短 commit sha(见 common.sh bolt_effective_ref),完整 sha 记入 detail;
    #    prebuilt 模式读镜像内 BUILD_VERSION 文件(可能为短 sha 或完整 sha)。
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 版本证据两级:镜像按约定提供的 BUILD_VERSION 文件 → 源码树 git HEAD
        # (镜像常为完整 clone+checkout 检出,短 sha 即版本基线)。
        if [[ -n "${BOLT_HOME:-}" && -f "${BOLT_HOME}/BUILD_VERSION" ]]; then
            actual="$(tr -d '\r\n' <"${BOLT_HOME}/BUILD_VERSION")"
            commit=""
        elif [[ -n "${BOLT_HOME:-}" ]]; then
            actual="$(bolt_effective_ref "${BOLT_HOME}" 2>/dev/null || true)"
            commit="$(bolt_full_commit "${BOLT_HOME}" 2>/dev/null || true)"
        else
            actual=""
            commit=""
        fi
    else
        if [[ -d "${BOLT_SOURCE_DIR}/.git" ]]; then
            actual="$(bolt_effective_ref 2>/dev/null || true)"
            commit="$(bolt_full_commit 2>/dev/null || true)"
        else
            actual=""
        fi
    fi
    if bolt_version_accepts "${actual}"; then
        version_status="passed"
        if [[ -n "${commit}" ]]; then
            version_detail="ref=${actual}, commit=${commit}"
        else
            version_detail="BUILD_VERSION=${actual}"
        fi
    else
        version_detail="Bolt ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游自带 gtest 单测子集(经 ctest 过滤运行)。
    #    build 模式:install 阶段用上游 release_with_test 目标编译了测试,必须真实存在,
    #    0 测试视为构建异常(failed);prebuilt 模式:镜像未随带测试树记 skipped
    #    (镜像不含测试树不是被测软件的功能缺陷)。
    local ctest_tree test_count
    ctest_tree="$(find_ctest_tree || true)"
    if [[ -n "${ctest_tree}" ]] && command -v ctest >/dev/null 2>&1; then
        test_count="$(ctest --test-dir "${ctest_tree}" -N -R "${BOLT_UPSTREAM_TEST_FILTER}" 2>/dev/null |
            sed -n 's/^Total Tests: //p' | tail -n 1 || true)"
        if [[ -z "${test_count}" || "${test_count}" == "0" ]]; then
            # 严禁零测试空跑通过(ctest 对 0 测试返回 0,不设此防会假性 passed)。
            if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
                unit_status="skipped"
                unit_detail="skipped: prebuilt image ships no ctest tests for filter '${BOLT_UPSTREAM_TEST_FILTER}' (upstream 'make release' does not compile tests)"
            else
                unit_detail="no tests registered for filter '${BOLT_UPSTREAM_TEST_FILTER}' in ${ctest_tree}; expected them from the release_with_test build; see bolt_test.log"
            fi
        elif (cd "${ctest_tree}" && ctest -R "${BOLT_UPSTREAM_TEST_FILTER}" --output-on-failure -j "$(nproc 2>/dev/null || printf '1')") \
            >>"${RESULTS_DIR}/bolt_test.log" 2>&1; then
            unit_status="passed"
            unit_detail="ctest subset '${BOLT_UPSTREAM_TEST_FILTER}' passed (${test_count} tests) in ${ctest_tree}"
        else
            unit_detail="ctest subset '${BOLT_UPSTREAM_TEST_FILTER}' failed (${test_count} tests registered); see bolt_test.log"
        fi
    else
        if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
            unit_status="skipped"
            unit_detail="skipped: prebuilt image ships no ctest tree (upstream 'make release' does not compile tests)"
        else
            unit_detail="upstream test tree not available (no CMake/ctest build directory); prebuilt images ship no gtest tree"
        fi
    fi

    # 3) 最小可用性冒烟:构建产物 libbolt*.so 可被动态链接器加载;静态库(libbolt*.a)
    #    无动态加载语义,以 ar 归档完整性检查替代(conan 默认产物为静态库)。
    lib="$(find_bolt_lib || true)"
    if [[ -z "${lib}" ]]; then
        smoke_detail="no libbolt artifact found (libbolt*.so / libbolt*.a); full job execution needs the Spark/Gluten backend (out of scope)"
    elif [[ "${lib}" == *.a ]]; then
        if ar t "${lib}" >>"${RESULTS_DIR}/bolt_test.log" 2>&1; then
            smoke_status="passed"
            smoke_detail="static library archive is intact (ar t): ${lib} (full Spark/Gluten job execution is out of scope)"
        else
            smoke_detail="static library archive check failed: ${lib}; see bolt_test.log"
        fi
    elif ldd "${lib}" >>"${RESULTS_DIR}/bolt_test.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="native library loads cleanly: ${lib} (full Spark/Gluten job execution is out of scope)"
    else
        smoke_detail="native library failed to load: ${lib}; see bolt_test.log"
    fi

    # 只对 failed 判败(skipped 不影响整体,与 spark 用例的 skip 语义一致)。
    local overall="passed"
    if [[ "${version_status}" == "failed" || "${unit_status}" == "failed" || "${smoke_status}" == "failed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${unit_status}" "${unit_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream-unit-test=${unit_status} link-smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    log "cleaned Bolt task data"
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
Run the Bolt functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Bolt version / git ref (default: ${SOFTWARE_VERSION})
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