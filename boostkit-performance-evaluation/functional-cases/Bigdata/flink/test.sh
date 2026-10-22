#!/usr/bin/env bash
# Apache Flink 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 本地 standalone mini-cluster 冒烟 + 运行上游自带示例 job。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

write_flink_configuration() {
    local conf_file
    # Flink 2.0 起改用新配置文件 conf/config.yaml;1.x 只读 conf/flink-conf.yaml,
    # 写错文件名时所有配置(端口、内存)会被静默忽略,REST 不会在指定端口就绪。
    # 两个文件同为扁平 key: value 格式,内容可复用。
    local major="${SOFTWARE_VERSION%%.*}"
    if [[ "${major}" -lt 2 ]]; then
        conf_file="${FLINK_HOME}/conf/flink-conf.yaml"
    else
        conf_file="${FLINK_HOME}/conf/config.yaml"
    fi
    mkdir -p "${FLINK_RUNTIME_DIR}/logs" "${FLINK_RUNTIME_DIR}/pids"
    # env.java.opts.all:JDK 17+ 强封装下必须显式 --add-opens,否则 client 端(如 2.3.0
    # WordCount 的 DataStream V2 初始化)反射 java.util 内部字段会抛
    # InaccessibleObjectException。上游 bin/config.sh 会前置 -XX:+IgnoreUnrecognizedVMOptions,
    # Java 8/11 上这些参数被忽略,写入无害。
    cat >"${conf_file}" <<EOF
jobmanager.rpc.address: 127.0.0.1
jobmanager.bind-host: 127.0.0.1
jobmanager.rpc.port: ${FLINK_RPC_PORT}
taskmanager.bind-host: 127.0.0.1
taskmanager.rpc.port: ${FLINK_TASKMANAGER_RPC_PORT}
taskmanager.data.port: 0
rest.address: 127.0.0.1
rest.bind-address: 127.0.0.1
rest.port: ${FLINK_REST_PORT}
blob.server.port: ${FLINK_BLOB_PORT}
taskmanager.numberOfTaskSlots: ${FLINK_PARALLELISM}
parallelism.default: ${FLINK_PARALLELISM}
jobmanager.memory.process.size: 1024m
taskmanager.memory.process.size: 1024m
io.tmp.dirs: ${TMPDIR}
env.log.dir: ${FLINK_RUNTIME_DIR}/logs
env.pid.dir: ${FLINK_RUNTIME_DIR}/pids
env.java.opts.all: --add-opens=java.base/java.lang=ALL-UNNAMED --add-opens=java.base/java.util=ALL-UNNAMED --add-opens=java.base/java.io=ALL-UNNAMED --add-opens=java.base/java.net=ALL-UNNAMED --add-opens=java.base/java.nio=ALL-UNNAMED
EOF
}

# 查询 REST /overview 的 TaskManager 数量;REST 未就绪时返回非零。
fetch_taskmanagers() {
    python3 - "${FLINK_REST_URL}" <<'PY'
import json, sys
from urllib.request import urlopen

with urlopen(f"{sys.argv[1]}/overview", timeout=3) as response:
    print(json.load(response).get("taskmanagers", 0))
PY
}

wait_for_rest() {
    local attempt
    for attempt in {1..30}; do
        if fetch_taskmanagers >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    log "ERROR: Flink REST endpoint did not become ready: ${FLINK_REST_URL}"
    # 直接带出守护进程日志尾部,排障不必再翻产物(cleanup 会删 runtime 目录)。
    tail -n 40 "${FLINK_RUNTIME_DIR}"/logs/*.log >>"${RESULTS_DIR}/flink_cluster.log" 2>/dev/null || true
    return 40
}

# 收集 Flink 守护进程自身的日志到结果目录(REST 不就绪/示例失败时的关键证据)。
# 必须在 check 阶段完成:func_cleanup 会删除 FLINK_RUNTIME_DIR。
collect_daemon_logs() {
    local f
    [[ -d "${FLINK_RUNTIME_DIR}/logs" ]] || return 0
    shopt -s nullglob
    for f in "${FLINK_RUNTIME_DIR}/logs"/*; do
        cp -a "${f}" "${RESULTS_DIR}/" 2>/dev/null || true
    done
    shopt -u nullglob
}

wait_for_taskmanager() {
    local attempt count
    for attempt in {1..60}; do
        count="$(fetch_taskmanagers 2>/dev/null || printf '0')"
        if [[ "${count:-0}" -ge 1 ]]; then
            printf '%s\n' "${count}"
            return 0
        fi
        sleep 1
    done
    printf '0\n'
    return 40
}

start_local_cluster() {
    write_flink_configuration || return 40
    "${FLINK_HOME}/bin/jobmanager.sh" start >>"${RESULTS_DIR}/flink_cluster.log" 2>&1 || {
        log "ERROR: failed to start the Flink JobManager"
        return 40
    }
    wait_for_rest || return $?
    "${FLINK_HOME}/bin/taskmanager.sh" start >>"${RESULTS_DIR}/flink_cluster.log" 2>&1 || {
        log "ERROR: failed to start the Flink TaskManager"
        return 40
    }
}

stop_local_cluster() {
    [[ -n "${FLINK_HOME}" && -x "${FLINK_HOME}/bin/taskmanager.sh" ]] || return 0
    "${FLINK_HOME}/bin/taskmanager.sh" stop-all >/dev/null 2>&1 || true
    "${FLINK_HOME}/bin/jobmanager.sh" stop >/dev/null 2>&1 || true
}

# 运行上游自带的 WordCount 示例(随官方发行版分发),断言作业成功结束并产出结果。
run_upstream_example() {
    local jar input output
    jar="$(find "${FLINK_HOME}/examples" -type f -name 'WordCount.jar' 2>/dev/null | sort | head -n 1 || true)"
    [[ -n "${jar}" ]] || {
        log "ERROR: bundled WordCount example jar not found under ${FLINK_HOME}/examples"
        return 40
    }
    input="${PERF_WORK_DIR}/wordcount-input.txt"
    output="${PERF_WORK_DIR}/wordcount-output"
    rm -rf -- "${output}"
    printf 'flink apache flink batch flink stream flink\n' >"${input}"
    if ! "${FLINK_HOME}/bin/flink" run "${jar}" --input "${input}" --output "${output}" \
        >>"${RESULTS_DIR}/upstream_example.log" 2>&1; then
        log "ERROR: upstream WordCount example failed"
        return 40
    fi
    if [[ ! -s "${output}" ]] || ! find "${output}" -type f -exec grep -qi 'flink' {} +; then
        log "ERROR: upstream WordCount example produced no countable output"
        return 40
    fi
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
        {"name": "upstream-example", "status": env("FUNC_U_STATUS"), "detail": env("FUNC_U_DETAIL")},
        {"name": "cluster-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    if [[ -z "${FLINK_HOME}" ]]; then
        FLINK_HOME="$(resolve_flink_home || true)"
    fi
    [[ -n "${FLINK_HOME}" && -x "${FLINK_HOME}/bin/flink" ]] || {
        log "ERROR: Apache Flink is unavailable"
        return 40
    }
    : >"${RESULTS_DIR}/flink_cluster.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual version_raw cluster_up=0

    # 1) 版本(统一 2>&1 合并输出后解析,避免上游把版本信息改到 stderr 时解析落空)
    version_raw="$("${FLINK_HOME}/bin/flink" --version 2>&1 || true)"
    actual="$(printf '%s\n' "${version_raw}" | sed -n 's/^Version: *\([^,]*\).*/\1/p' | head -n 1)"
    [[ -n "${actual}" ]] || actual="$(printf '%s\n' "${version_raw}" | sed -n 's/.*Flink \([0-9][0-9.]*\).*/\1/p' | head -n 1)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="version=${actual}"
    else
        version_detail="Apache Flink ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        # 保留原始输出片段便于定位镜像/环境问题。
        version_detail+="; flink --version output: $(printf '%s\n' "${version_raw}" | head -n 3 | tr '\n' ' ' | cut -c1-200)"
    fi

    # 2) 本地 standalone mini-cluster(JobManager + TaskManager)
    if start_local_cluster; then
        cluster_up=1
    else
        smoke_detail="failed to start the local standalone cluster; see flink_cluster.log"
    fi

    # 3) 上游自带示例 job(需 cluster 在线)
    if [[ "${cluster_up}" -eq 1 ]]; then
        if run_upstream_example; then
            upstream_status="passed"
            upstream_detail="bundled upstream WordCount example completed successfully"
        else
            upstream_detail="bundled upstream WordCount example failed; see upstream_example.log"
        fi

        local tm_count
        tm_count="$(wait_for_taskmanager || printf '0')"
        if [[ "${tm_count:-0}" -ge 1 ]]; then
            smoke_status="passed"
            smoke_detail="local standalone cluster is up with ${tm_count} TaskManager(s) at ${FLINK_REST_URL}"
        else
            smoke_detail="local standalone cluster started but no TaskManager registered"
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
    collect_daemon_logs
    log "checks: version=${version_status} upstream-example=${upstream_status} cluster-smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    stop_local_cluster
    [[ -d "${FLINK_RUNTIME_DIR}" ]] && rm -rf -- "${FLINK_RUNTIME_DIR}"
    # 删除本次运行的短路径工作树(仅限约定前缀,避免误删)。
    if [[ -n "${FLINK_RUN_ROOT:-}" && "${FLINK_RUN_ROOT}" == /tmp/flink-func-* && -d "${FLINK_RUN_ROOT}" ]]; then
        rm -rf -- "${FLINK_RUN_ROOT}"
    fi
    log "cleaned Apache Flink task data"
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
Run the Apache Flink functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Flink version (default: ${SOFTWARE_VERSION})
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