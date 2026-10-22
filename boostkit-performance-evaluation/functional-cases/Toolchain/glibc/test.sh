#!/usr/bin/env bash
# glibc 测试脚本:功能判定 + 收尾。
# 判定叠加版本校验、libc 程序编译运行与 getconf 版本报告。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 编译一个使用 libc(memcpy/memcmp/qsort/malloc/snprintf)的程序并运行。
# build 模式下用本次构建的加载器 + 私有库目录运行,验证被测 glibc。
run_libc_program() {
    local dir="${PERF_WORK_DIR}/smoke"
    [[ -n "${CC_BIN}" && -x "${CC_BIN}" ]] || {
        log "ERROR: no C compiler is available to build the libc program"
        return 1
    }
    mkdir -p "${dir}"
    cat >"${dir}/libc_prog.c" <<'C'
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int compare_int(const void *a, const void *b) {
    int x = *(const int *)a;
    int y = *(const int *)b;
    return (x > y) - (x < y);
}

int main(void) {
    const char *src = "glibc-functional";
    char dst[32];
    char buf[64];

    memcpy(dst, src, strlen(src) + 1);
    if (memcmp(dst, src, strlen(src)) != 0) {
        return 1;
    }

    int values[5] = {5, 1, 4, 2, 3};
    qsort(values, 5, sizeof(int), compare_int);
    for (int i = 0; i < 5; ++i) {
        if (values[i] != i + 1) {
            return 1;
        }
    }

    int *heap = malloc(10 * sizeof(int));
    if (heap == NULL) {
        return 1;
    }
    int total = 0;
    for (int i = 0; i < 10; ++i) {
        heap[i] = i * i;
        total += heap[i];
    }
    free(heap);
    if (total != 285) {
        return 1;
    }

    snprintf(buf, sizeof(buf), "%s-%d", dst, total);
    if (strcmp(buf, "glibc-functional-285") != 0) {
        return 1;
    }
    printf("glibc-libc-ok\n");
    return 0;
}
C
    local output
    output="$(
        cd "${dir}"
        "${CC_BIN}" -O2 -o libc_prog libc_prog.c
        if [[ -n "${GLIBC_LOADER}" && -x "${GLIBC_LOADER}" ]]; then
            # 用本次构建的加载器加载私有 glibc。
            "${GLIBC_LOADER}" --library-path "${GLIBC_LIB_DIR}" ./libc_prog
        else
            ./libc_prog
        fi
    )" || return 1
    # 用 bash 模式匹配,避免依赖裁剪镜像里可能缺失的 grep。
    [[ "${output}" == *glibc-libc-ok* ]]
}

# 用被测 glibc 的 getconf 报告版本:私有前缀的 getconf 通过被测加载器运行,
# 系统 glibc 即被测对象(层2)时才直接用系统 getconf,避免把系统版本当成本次被测版本。
reported_glibc_version() {
    local getconf_bin line
    getconf_bin="$(resolve_glibc_getconf || true)"
    if [[ -n "${getconf_bin}" && -n "${GLIBC_LOADER}" && -x "${GLIBC_LOADER}" ]]; then
        line="$("${GLIBC_LOADER}" --library-path "${GLIBC_LIB_DIR}" "${getconf_bin}" GNU_LIBC_VERSION 2>/dev/null || true)"
    else
        line="$(getconf GNU_LIBC_VERSION 2>/dev/null || true)"
    fi
    # 输出形如 "glibc 2.43",版本号是最后一个字段。
    normalize_version "${line##* }"
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
    mkdir -p "${RESULTS_DIR}"
    : >"${RESULTS_DIR}/check.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual reported

    # 1) 版本
    actual="$(glibc_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="glibc ${actual}"
    else
        version_detail="glibc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游测试(替代):libc 程序编译运行。
    if run_libc_program >>"${RESULTS_DIR}/check.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="libc program compiled and ran (loader=${GLIBC_LOADER:-system})"
    else
        upstream_detail="libc program compile/run failed; see check.log"
    fi

    # 3) 基础功能 smoke:被测 glibc 的 getconf 报告版本。
    reported="$(reported_glibc_version)"
    if [[ "${reported}" == "${SOFTWARE_VERSION}" ]]; then
        smoke_status="passed"
        smoke_detail="getconf GNU_LIBC_VERSION = ${reported}"
    else
        smoke_detail="getconf reported ${reported:-unknown}; see check.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    # 判定摘要写入 check.log:成功路径下上游测试输出为空,不写摘要会让产物为 0 字节,
    # 框架会把它当成 missing output。
    {
        printf 'checks: version=%s upstream=%s smoke=%s\n' "${version_status}" "${upstream_status}" "${smoke_status}"
        printf 'version: %s (expected %s)\n' "${actual:-unknown}" "${SOFTWARE_VERSION}"
        printf 'loader: %s\n' "${GLIBC_LOADER:-system}"
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
    log "cleaned glibc task data"
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
Run the glibc functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    glibc version (default: ${SOFTWARE_VERSION})
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