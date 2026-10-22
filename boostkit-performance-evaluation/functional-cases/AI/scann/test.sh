#!/usr/bin/env bash
# scann 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + dot_product 检索 smoke + squared_l2 检索 smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 基础 smoke:dot_product 建索引并检索,校验返回形状。
write_dot_product_smoke() {
    cat >"$1" <<'PY'
import numpy as np
import scann

rng = np.random.default_rng(0)
vectors = rng.random((32, 8), dtype="float32")

searcher = (
    scann.scann_ops_pybind.builder(vectors, 3, "dot_product")
    .tree(8, 1, training_sample_size=32, spherical=True, quantize_centroids=True)
    .score_ah(2, anisotropic_quantization_threshold=0.2)
    .reorder(3)
    .build()
)
neighbors, distances = searcher.search(vectors[0], 3, 3, 1)
assert neighbors.shape == (3,), neighbors.shape
assert distances.shape == (3,), distances.shape
assert (neighbors >= 0).all(), "invalid neighbour id"
print("scann dot_product build/search ok")
PY
}

# 更完整的功能:squared_l2 建索引并检索,校验返回形状一致、邻居合法与距离升序。
write_squared_l2_smoke() {
    cat >"$1" <<'PY'
import numpy as np
import scann

rng = np.random.default_rng(1)
vectors = rng.random((32, 8), dtype="float32")

searcher = (
    scann.scann_ops_pybind.builder(vectors, 3, "squared_l2")
    .tree(8, 1, training_sample_size=32, spherical=False)
    .score_ah(2)
    .reorder(3)
    .build()
)
neighbors, distances = searcher.search(vectors[0], 3, 3, 1)
# ScaNN 只保证"至多返回 k 个":候选不足时(如查询落在小叶子)结果会少于 k,
# 故只校验形状一致、邻居数量区间、id 合法与距离升序。
assert neighbors.shape == distances.shape, (neighbors.shape, distances.shape)
assert 0 < neighbors.shape[0] <= 3, neighbors.shape
assert (neighbors >= 0).all() and (neighbors < vectors.shape[0]).all(), "invalid neighbour id"
assert (np.diff(distances) >= -1e-5).all(), f"distances not ascending: {distances}"
print("scann squared_l2 build/search ok")
PY
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" d_status="$4" d_detail="$5" l_status="$6" l_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_D_STATUS="${d_status}" FUNC_D_DETAIL="${d_detail}" \
        FUNC_L_STATUS="${l_status}" FUNC_L_DETAIL="${l_detail}" \
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
        {"name": "scann-dot-product", "status": env("FUNC_D_STATUS"), "detail": env("FUNC_D_DETAIL")},
        {"name": "scann-squared-l2", "status": env("FUNC_L_STATUS"), "detail": env("FUNC_L_DETAIL")},
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
    [[ -n "${PYBIN}" && -x "${PYBIN}" ]] || {
        log "ERROR: python interpreter is unavailable"
        return 40
    }
    local log_file="${RESULTS_DIR}/functional_test.log"
    : >"${log_file}"

    local version_status="failed" version_detail="" actual
    local dot_status="failed" dot_detail=""
    local l2_status="failed" l2_detail=""

    # 1) 版本
    actual="$(read_installed_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="scann ${actual}"
    else
        version_detail="scann ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi
    printf 'version: %s (%s)\n' "${version_status}" "${version_detail}" >>"${log_file}"

    # 2) 基础 smoke:dot_product
    if write_dot_product_smoke "${TMPDIR}/scann_dot.py" && \
        "${PYBIN}" "${TMPDIR}/scann_dot.py" >>"${log_file}" 2>&1; then
        dot_status="passed"
        dot_detail="dot_product searcher build/search returned expected shapes"
    else
        dot_detail="dot_product smoke failed; see functional_test.log"
    fi
    printf 'scann-dot-product: %s (%s)\n' "${dot_status}" "${dot_detail}" >>"${log_file}"

    # 3) squared_l2 检索
    if write_squared_l2_smoke "${TMPDIR}/scann_l2.py" && \
        "${PYBIN}" "${TMPDIR}/scann_l2.py" >>"${log_file}" 2>&1; then
        l2_status="passed"
        l2_detail="squared_l2 searcher build/search returned consistent shapes and sorted distances"
    else
        l2_detail="squared_l2 smoke failed; see functional_test.log"
    fi
    printf 'scann-squared-l2: %s (%s)\n' "${l2_status}" "${l2_detail}" >>"${log_file}"

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${dot_status}" != "passed" || "${l2_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${dot_status}" "${dot_detail}" \
        "${l2_status}" "${l2_detail}"
    log "checks: version=${version_status} scann-dot-product=${dot_status} scann-squared-l2=${l2_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        log "prebuilt mode: nothing installed by this case to clean"
    else
        [[ -d "${VENV_DIR}" ]] && rm -rf -- "${VENV_DIR}"
        [[ -d "${BUILD_DIR}" ]] && rm -rf -- "${BUILD_DIR}"
        log "removed the task-private venv and build directory"
    fi
    [[ -d "${TMPDIR}" ]] && rm -rf -- "${TMPDIR}/scann_dot.py" "${TMPDIR}/scann_l2.py"
    log "cleaned scann task data"
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
Run the ScaNN functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    ScaNN version (default: ${SOFTWARE_VERSION})
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