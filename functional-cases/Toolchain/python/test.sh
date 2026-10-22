#!/usr/bin/env bash
# python (CPython) 测试脚本:功能判定 + 收尾。
# 判定优先复用上游回归测试(test.regrtest 子集),叠加版本校验与 compileall smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 基础功能 smoke:compileall 一个临时包目录,再运行内建模块断言。
run_python_smoke() {
    local dir="${PERF_WORK_DIR}/smoke"
    rm -rf -- "${dir}"
    mkdir -p "${dir}/pkg"
    cat >"${dir}/pkg/__init__.py" <<'PY'
VALUE = 41
PY
    cat >"${dir}/pkg/mod.py" <<'PY'
def compute(n):
    return n + 1
PY
    "${PYTHON_BIN}" -m compileall -q "${dir}" || return 1
    # 用解释器自身确认字节码产物,避免依赖镜像里可能缺失的 find。
    "${PYTHON_BIN}" - "${dir}" <<'PY' || return 1
import pathlib
import sys

sys.exit(0 if next(pathlib.Path(sys.argv[1]).rglob("*.pyc"), None) else 1)
PY
    "${PYTHON_BIN}" - "$SOFTWARE_VERSION" "${dir}" <<'PY' || return 1
import sys

expected = tuple(int(part) for part in sys.argv[1].split(".")[:2])
assert sys.version_info[:2] == expected, (sys.version_info[:2], expected)
sys.path.insert(0, sys.argv[2])
import hashlib
import json
import zlib
from pkg.mod import compute

assert compute(41) == 42
assert json.loads(json.dumps({"values": [1, 2, 3]}))["values"] == [1, 2, 3]
assert hashlib.sha256(b"abc").hexdigest().startswith("ba7816bf")
assert zlib.decompress(zlib.compress(b"data" * 100)) == b"data" * 100
print("python-smoke-ok")
PY
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
    [[ -x "${PYTHON_BIN}" ]] || {
        log "ERROR: python interpreter is unavailable: ${PYTHON_BIN:-unset}"
        return 40
    }
    mkdir -p "${RESULTS_DIR}"
    : >"${RESULTS_DIR}/check.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(python_version_of "${PYTHON_BIN}")"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="Python ${actual}"
    else
        version_detail="Python ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游测试:CPython 官方回归测试子集。
    if "${PYTHON_BIN}" -m test -q test_math test_json >>"${RESULTS_DIR}/check.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="regrtest test_math/test_json passed"
    else
        upstream_detail="regrtest test_math/test_json failed; see check.log"
    fi

    # 3) 基础功能 smoke:compileall + 内建模块断言。
    if run_python_smoke >>"${RESULTS_DIR}/check.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="compileall + stdlib assertions ok"
    else
        smoke_detail="compileall/stdlib smoke failed; see check.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
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
    log "cleaned python task data"
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
Run the CPython functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    CPython version (default: ${SOFTWARE_VERSION})
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