#!/usr/bin/env bash
# gcc 测试脚本:功能判定 + 收尾。
# 判定叠加版本校验、C 程序编译运行与 C++ 程序编译运行。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 编译并运行一个 C 程序。
run_c_compile() {
    local dir="${PERF_WORK_DIR}/smoke"
    mkdir -p "${dir}"
    cat >"${dir}/hello.c" <<'C'
#include <stdio.h>

int main(void) {
    printf("hello-gcc\n");
    return 0;
}
C
    (
        cd "${dir}"
        "${GCC_BIN}" -O2 -o hello_c hello.c
        ./hello_c | grep -qx hello-gcc
    )
}

# 编译并运行一个 C++ 程序。
run_cxx_compile() {
    local dir="${PERF_WORK_DIR}/smoke" libstdcxx candidate libdir=""
    mkdir -p "${dir}"
    cat >"${dir}/hello.cpp" <<'CPP'
#include <iostream>
#include <numeric>
#include <vector>

int main() {
    std::vector<int> values{1, 2, 3, 4};
    int total = std::accumulate(values.begin(), values.end(), 0);
    std::cout << "hello-gccxx-" << total << std::endl;
    return 0;
}
CPP
    # 预构建镜像把 gcc 装在 /usr/local/gcc,自带的新版 libstdc++ 不在系统默认库路径上;
    # 直接运行会命中系统旧 libstdc++ 并报 GLIBCXX_x.y.z not found。
    # 用编译器自报的 libstdc++ 路径作为运行期库目录。
    for candidate in libstdc++.so.6 libstdc++.so; do
        libstdcxx="$("${GXX_BIN}" -print-file-name="${candidate}" 2>/dev/null || true)"
        [[ "${libstdcxx}" == /* && -e "${libstdcxx}" ]] || continue
        libdir="$(dirname "${libstdcxx}")"
        break
    done
    (
        cd "${dir}"
        "${GXX_BIN}" -std=c++17 -O2 -o hello_cxx hello.cpp
        LD_LIBRARY_PATH="${libdir:+${libdir}:}${LD_LIBRARY_PATH:-}" ./hello_cxx | grep -qx hello-gccxx-10
    )
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
    [[ -x "${GCC_BIN}" && -x "${GXX_BIN}" ]] || {
        log "ERROR: gcc/g++ are unavailable: ${GCC_BIN:-unset} ${GXX_BIN:-unset}"
        return 40
    }
    mkdir -p "${RESULTS_DIR}"
    : >"${RESULTS_DIR}/check.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(gcc_version_of "${GCC_BIN}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="gcc ${actual}"
    else
        version_detail="gcc ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游测试(替代):编译运行 C 程序。
    if run_c_compile >>"${RESULTS_DIR}/check.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="gcc compiled and ran hello.c"
    else
        upstream_detail="gcc C compile/run failed; see check.log"
    fi

    # 3) 基础功能 smoke:编译运行 C++ 程序。
    if run_cxx_compile >>"${RESULTS_DIR}/check.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="g++ compiled and ran hello.cpp (C++17)"
    else
        smoke_detail="g++ C++ compile/run failed; see check.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    # 判定摘要写入 check.log:编译运行成功时没有输出,不写摘要会让产物为 0 字节,
    # 框架会把它当成 missing output。
    {
        printf 'checks: version=%s upstream=%s smoke=%s\n' "${version_status}" "${upstream_status}" "${smoke_status}"
        printf 'gcc: %s (expected %s)\n' "${actual:-unknown}" "${SOFTWARE_VERSION}"
        printf 'gcc_bin: %s\n' "${GCC_BIN:-unset}"
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
    log "cleaned gcc task data"
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
Run the gcc functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    gcc version (default: ${SOFTWARE_VERSION})
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