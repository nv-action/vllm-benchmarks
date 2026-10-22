#!/usr/bin/env bash
# Apache Spark 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 运行官方示例(SparkPi) + pyspark DataFrame smoke(spark.range(10).count())。
#       pyspark-smoke 在 Spark<3.5 且容器 Python≥3.13 的组合下跳过(3.3/3.4 的 pyspark
#       依赖 typing.io,Python 3.13 起移除;上游核实 v3.5.2 起不再使用),检查项状态记为
#       skipped,不影响整体通过。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 运行官方发行版自带的 SparkPi 示例(local 模式),断言作业成功并输出 Pi 估计值。
run_upstream_example() {
    local jar
    jar="$(find "${SPARK_HOME}/examples/jars" -type f -name 'spark-examples_*.jar' 2>/dev/null | sort | head -n 1 || true)"
    [[ -n "${jar}" ]] || {
        log "ERROR: bundled SparkPi example jar not found under ${SPARK_HOME}/examples/jars"
        return 40
    }
    if ! "${SPARK_HOME}/bin/spark-submit" --master "${SPARK_LOCAL_MASTER}" \
        --conf "spark.local.dir=${SPARK_LOCAL_DIRS}" \
        --class org.apache.spark.examples.SparkPi "${jar}" 10 \
        >>"${RESULTS_DIR}/spark_example.log" 2>&1; then
        log "ERROR: upstream SparkPi example failed"
        return 40
    fi
    grep -q "Pi is roughly" "${RESULTS_DIR}/spark_example.log" || {
        log "ERROR: upstream SparkPi example produced no Pi estimate"
        return 40
    }
}

# 用 pyspark DataFrame API 做最小功能冒烟:断言 spark.range(10).count() == 10。
run_pyspark_smoke() {
    local script="${PERF_WORK_DIR}/spark_smoke.py" out="${RESULTS_DIR}/spark_smoke.log"
    cat >"${script}" <<'PY'
from pyspark.sql import SparkSession

spark = SparkSession.builder.appName("spark-func-smoke").getOrCreate()
count = spark.range(10).count()
print(f"spark.range(10).count()={count}")
assert count == 10, f"unexpected count: {count}"
spark.stop()
PY
    export PYSPARK_PYTHON="${PYSPARK_PYTHON:-python3}"
    if ! "${SPARK_HOME}/bin/spark-submit" --master "${SPARK_LOCAL_MASTER}" \
        --conf "spark.local.dir=${SPARK_LOCAL_DIRS}" "${script}" >"${out}" 2>&1; then
        log "ERROR: pyspark DataFrame smoke failed"
        return 40
    fi
    grep -q "spark.range(10).count()=10" "${out}" || {
        log "ERROR: pyspark DataFrame smoke returned an unexpected result"
        return 40
    }
}

# 已知的结构性不兼容:Spark 3.5 之前的 pyspark 依赖 typing.io,而 Python 3.13 起移除了
# typing.io(上游核实:v3.4.3 的 pyspark/broadcast.py 仍 import typing.io,v3.5.2 起移除)。
# 这种组合下 pyspark-smoke 注定失败,跳过而不是报 failed(其余组合照常执行)。
pyspark_python_incompatible() {
    # Spark 版本 < 3.5(sort -V 比较,3.5+/4.x/未来版本均不命中)。
    [[ "$(printf '%s\n' "${SOFTWARE_VERSION}" "3.5" | sort -V | head -n 1)" == "${SOFTWARE_VERSION}" ]] || return 1
    # 且容器 Python >= 3.13(以解释器自身的版本比较为准)。
    python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 13) else 1)' 2>/dev/null
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
        {"name": "pyspark-smoke", "status": env("FUNC_C_STATUS"), "detail": env("FUNC_C_DETAIL")},
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
    if [[ -z "${SPARK_HOME}" ]]; then
        SPARK_HOME="$(resolve_spark_home || true)"
    fi
    [[ -n "${SPARK_HOME}" && -x "${SPARK_HOME}/bin/spark-submit" ]] || {
        log "ERROR: Apache Spark is unavailable"
        return 40
    }
    : >"${RESULTS_DIR}/spark_example.log"

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual version_raw

    # 1) 版本(spark-submit 的版本信息走 stderr,必须 2>&1 合并后才能解析)
    version_raw="$("${SPARK_HOME}/bin/spark-submit" --version 2>&1 || true)"
    actual="$(printf '%s\n' "${version_raw}" | sed -n 's/.*[Vv]ersion \([0-9][0-9.]*\).*/\1/p' | head -n 1)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="version=${actual}"
    else
        version_detail="Apache Spark ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        # 保留原始输出片段便于定位(例如镜像内 Java 过低时会出现 UnsupportedClassVersionError)。
        version_detail+="; spark-submit --version output: $(printf '%s\n' "${version_raw}" | head -n 3 | tr '\n' ' ' | cut -c1-200)"
    fi

    # 2) 上游自带示例(SparkPi)
    if run_upstream_example; then
        upstream_status="passed"
        upstream_detail="bundled upstream SparkPi example completed successfully"
    else
        upstream_detail="bundled upstream SparkPi example failed; see spark_example.log"
    fi

    # 3) pyspark DataFrame 冒烟(已知的 Spark<3.5 × Python≥3.13 结构性不兼容时跳过)
    if pyspark_python_incompatible; then
        smoke_status="skipped"
        smoke_detail="skipped: Spark ${SOFTWARE_VERSION} bundled pyspark imports typing.io, removed in Python 3.13+ (container python: $(python3 --version 2>/dev/null | awk '{print $2}' || true))"
    elif run_pyspark_smoke; then
        smoke_status="passed"
        smoke_detail="pyspark spark.range(10).count() == 10"
    else
        smoke_detail="pyspark DataFrame smoke failed; see spark_smoke.log"
    fi

    local overall="passed"
    if [[ "${version_status}" == "failed" || "${upstream_status}" == "failed" || "${smoke_status}" == "failed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream-example=${upstream_status} pyspark-smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${SPARK_LOCAL_DIRS}" ]] && rm -rf -- "${SPARK_LOCAL_DIRS}"
    log "cleaned Apache Spark task data"
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
Run the Apache Spark functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    Spark version (default: ${SOFTWARE_VERSION})
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