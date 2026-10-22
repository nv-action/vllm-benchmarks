#!/usr/bin/env bash
# MySQL 测试脚本:功能判定 + 收尾。
# 判定采用:版本校验 + 上游 mysqlslap 客户端烟测 + 基础 SQL 回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 一次性实例的启动/关闭 ----------

# 从 mysqld --version 解析三段式版本号。
mysqld_version() {
    local binary="$1"
    [[ -x "${binary}" ]] || return 1
    "${binary}" --version 2>/dev/null | sed -nE 's/.*Ver ([0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n 1
}

# 通过私有 socket 配置本任务账号(root@'%'),供 TCP 烟测使用。
# 这里必须摘掉 MYSQL_HOST:它是本用例为 TCP 烟测 export 的,会让客户端把这次的 --socket
# 调用判成 TCP(端口还回落到默认 3306,客户端不认 MYSQL_PORT);而保留 host 同时又指定
# --protocol=socket 会被判 "Wrong or unknown protocol"(2047)。
# 用 env -u 摘掉后,客户端按默认规则走:host 为空 → localhost → unix socket。
configure_instance_account() {
    env -u MYSQL_HOST "${MYSQL_BIN}" --socket="${SOCKET_PATH}" -u root <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS func_smoke;
FLUSH PRIVILEGES;
SQL
}

# 在任务私有目录初始化并拉起一次性 mysqld。
start_mysql_instance() {
    local attempt mysqld_user
    mysqld_user="$(id -un)"
    if [[ "$(id -u)" -eq 0 ]]; then
        mysqld_user="root"
    fi
    rm -rf -- "${DATADIR}" "${MYSQL_TMP_DIR}"
    mkdir -p "${DATADIR}" "${MYSQL_TMP_DIR}"

    "${MYSQLD_BIN}" --no-defaults --initialize-insecure \
        --basedir="${MYSQL_BASE_DIR}" \
        --datadir="${DATADIR}" \
        --user="${mysqld_user}" \
        --log-error="${ERR_LOG}" || {
        log "ERROR: mysqld data directory initialization failed (see ${ERR_LOG})"
        return 40
    }

    "${MYSQLD_BIN}" --no-defaults --daemonize \
        --basedir="${MYSQL_BASE_DIR}" \
        --datadir="${DATADIR}" \
        --tmpdir="${MYSQL_TMP_DIR}" \
        --user="${mysqld_user}" \
        --socket="${SOCKET_PATH}" \
        --pid-file="${PID_FILE}" \
        --port="${MYSQL_PORT}" \
        --bind-address="${MYSQL_HOST}" \
        --log-error="${ERR_LOG}" || {
        log "ERROR: failed to launch mysqld (see ${ERR_LOG})"
        return 40
    }

    for attempt in {1..60}; do
        if env -u MYSQL_HOST "${MYSQLADMIN_BIN}" --socket="${SOCKET_PATH}" -u root ping >/dev/null 2>&1; then
            configure_instance_account || {
                log "ERROR: failed to configure the smoke account"
                return 40
            }
            log "mysqld is ready on port ${MYSQL_PORT} (socket ${SOCKET_PATH})"
            return 0
        fi
        sleep 1
    done
    log "ERROR: mysqld did not become ready (see ${ERR_LOG})"
    return 40
}

shutdown_mysql_instance() {
    local attempt
    [[ -S "${SOCKET_PATH}" ]] || return 0
    env -u MYSQL_HOST MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQLADMIN_BIN}" \
        --socket="${SOCKET_PATH}" -u root shutdown >/dev/null 2>&1 || true
    for attempt in {1..40}; do
        [[ -S "${SOCKET_PATH}" ]] || return 0
        sleep 0.5
    done
    return 0
}

# ---------- 阶段:check ----------

# 上游 mysqlslap 客户端:自动生成 SQL 并施加并发负载(官方随发行版提供)。
run_upstream_test() {
    local mtr
    if [[ -x "${MYSQLSLAP_BIN}" ]]; then
        MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQLSLAP_BIN}" \
            "--host=${MYSQL_HOST}" "--port=${MYSQL_PORT}" "--user=${MYSQL_DB_USER}" \
            --concurrency=4 --iterations=2 --number-of-queries=200 \
            --auto-generate-sql --auto-generate-sql-load-type=mixed
        return $?
    fi
    # 退化路径:若镜像内置 mysql-test-run.pl,则跑一个最小的 main 套件用例。
    mtr="${MYSQL_BASE_DIR}/mysql-test/mysql-test-run.pl"
    if [[ -x "${mtr}" || -f "${mtr}" ]]; then
        (cd "${MYSQL_BASE_DIR}/mysql-test" && perl "${mtr}" --suite=main --do-test=main.1st --force) 
        return $?
    fi
    log "ERROR: neither mysqlslap nor mysql-test-run.pl is available in the software image"
    return 40
}

# 基础功能 smoke:建库建表、写入并读回校验。
run_sql_roundtrip() {
    local got
    MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" --protocol=TCP <<SQL
CREATE DATABASE IF NOT EXISTS func_smoke;
USE func_smoke;
CREATE TABLE IF NOT EXISTS func_t (id INT PRIMARY KEY, v VARCHAR(32));
DELETE FROM func_t;
INSERT INTO func_t (id, v) VALUES (1, 'func_value');
SQL
    got="$(MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
        "-h${MYSQL_HOST}" "-P${MYSQL_PORT}" "-u${MYSQL_DB_USER}" --protocol=TCP -N -B \
        -e 'SELECT v FROM func_smoke.func_t WHERE id=1')"
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
    [[ -x "${MYSQLD_BIN}" && -x "${MYSQL_BIN}" ]] || {
        log "ERROR: MySQL binaries are unavailable; did the install stage run?"
        return 40
    }
    mkdir -p "${SERVICE_DIR}"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(mysqld_version "${MYSQLD_BIN}" || true)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="mysqld Ver ${actual}"
    else
        version_detail="mysqld ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 3) 需要一次性实例;启动失败则两项都记失败并说明原因。
    if start_mysql_instance >"${RESULTS_DIR}/service_start.log" 2>&1; then
        if run_upstream_test >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
            upstream_status="passed"
            upstream_detail="upstream mysqlslap load client completed on port ${MYSQL_PORT}"
        else
            upstream_detail="upstream test failed; see upstream_test.log"
        fi
        if run_sql_roundtrip >"${RESULTS_DIR}/sql_roundtrip.log" 2>&1; then
            smoke_status="passed"
            smoke_detail="CREATE/INSERT/SELECT roundtrip ok on port ${MYSQL_PORT}"
        else
            smoke_detail="SQL roundtrip failed; see sql_roundtrip.log"
        fi
    else
        upstream_detail="mysql instance did not start; see service_start.log"
        smoke_detail="mysql instance did not start; see service_start.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream=${upstream_status} smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    shutdown_mysql_instance || true
    [[ -d "${SERVICE_DIR}" ]] && rm -rf -- "${SERVICE_DIR}"
    log "cleaned mysql task data"
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
Run the MySQL functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    MySQL version (default: ${SOFTWARE_VERSION})
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