#!/usr/bin/env bash
# OceanBase 测试脚本:功能判定 + 收尾。
# 判定采用:版本校验 + 上游 sysbench OLTP 点查烟测 + 基础 SQL 回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 上游 sysbench:建库 + 点查用例 prepare/run(只读为主的轻量负载烟测)。
run_sysbench_smoke() {
    command -v sysbench >/dev/null 2>&1 || {
        log "ERROR: sysbench is required for the OceanBase LTP smoke"
        return 40
    }
    ob_mysql -e "CREATE DATABASE IF NOT EXISTS func_sb" || return 40
    local -a args=(
        sysbench --db-driver=mysql
        "--mysql-host=${OB_HOST}" "--mysql-port=${OB_PORT}" "--mysql-user=${OB_USER}"
        --mysql-db=func_sb
        "--tables=${OB_SYSBENCH_TABLES}" "--table-size=${OB_SYSBENCH_TABLE_SIZE}"
    )
    [[ -n "${OB_PASSWORD}" ]] && args+=("--mysql-password=${OB_PASSWORD}")
    log "sysbench oltp_point_select prepare"
    "${args[@]}" oltp_point_select prepare || return 40
    log "sysbench oltp_point_select run (threads=${OB_SYSBENCH_THREADS}, time=${OB_SYSBENCH_TIME})"
    "${args[@]}" "--threads=${OB_SYSBENCH_THREADS}" "--time=${OB_SYSBENCH_TIME}" \
        --report-interval=1 oltp_point_select run || return 40
}

# 基础功能 smoke:建库建表、写入并读回校验。
run_sql_roundtrip() {
    local got
    ob_mysql <<SQL
CREATE DATABASE IF NOT EXISTS func_smoke;
USE func_smoke;
CREATE TABLE IF NOT EXISTS func_t (id INT PRIMARY KEY, v VARCHAR(32));
DELETE FROM func_t;
INSERT INTO func_t (id, v) VALUES (1, 'func_value');
SQL
    got="$(ob_mysql -N -B -s -e 'SELECT v FROM func_smoke.func_t WHERE id=1')"
    [[ "${got}" == "func_value" ]] || {
        log "ERROR: SQL roundtrip returned '${got}' instead of 'func_value'"
        return 40
    }
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" u_status="$4" u_detail="$5" s_status="$6" s_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_U_STATUS="${u_status}" FUNC_U_DETAIL="${u_detail}" \
        FUNC_S_STATUS="${s_status}" FUNC_S_DETAIL="${s_detail}" \
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
        {"name": "sql-roundtrip", "status": env("FUNC_S_STATUS"), "detail": env("FUNC_S_DETAIL")},
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
    mkdir -p "${RESULTS_DIR}"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    if ! check_observer_ready; then
        version_detail="observer is not reachable on ${OB_HOST}:${OB_PORT}; did the install stage deploy it?"
        upstream_detail="${version_detail}"
        smoke_detail="${version_detail}"
    else
        # 1) 版本
        actual="$(query_observer_version)"
        if version_matches "${actual}" "${SOFTWARE_VERSION}"; then
            version_status="passed"
            version_detail="observer ${actual}"
        else
            version_detail="observer ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        fi

        # 2) 上游 sysbench 点查烟测
        if run_sysbench_smoke >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
            upstream_status="passed"
            upstream_detail="upstream sysbench oltp_point_select ok (threads=${OB_SYSBENCH_THREADS})"
        else
            upstream_detail="upstream sysbench smoke failed; see upstream_test.log"
        fi

        # 3) 基础 SQL 回环
        if run_sql_roundtrip >"${RESULTS_DIR}/sql_roundtrip.log" 2>&1; then
            smoke_status="passed"
            smoke_detail="CREATE/INSERT/SELECT roundtrip ok on port ${OB_PORT}"
        else
            smoke_detail="SQL roundtrip failed; see sql_roundtrip.log"
        fi
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream=${upstream_status} sql=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    # 与启动同一条路径:oceanbase-service.sh 通过 obshell 停止 observer。
    if [[ -f "${OB_SERVICE_SCRIPT}" ]]; then
        bash "${OB_SERVICE_SCRIPT}" stop >/dev/null 2>&1 || true
    fi
    rm -rf -- "${OB_DATA_DIR}" "${OB_REDO_DIR}"
    log "stopped the oceanbase observer and removed task data"
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
Run the OceanBase functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    OceanBase version (default: ${SOFTWARE_VERSION})
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