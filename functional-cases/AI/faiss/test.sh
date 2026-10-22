#!/usr/bin/env bash
# faiss 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + IndexFlatL2 精确检索 smoke + IndexIVFPQ 训练/检索 smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 基础 smoke:构建 IndexFlatL2,add 向量,search 返回最近邻并断言形状/结果正确。
write_flat_l2_smoke() {
    cat >"$1" <<'PY'
import numpy as np
import faiss

rng = np.random.default_rng(0)
d, nb, nq, k = 64, 1000, 5, 4
xb = rng.random((nb, d), dtype="float32")
xq = rng.random((nq, d), dtype="float32")

index = faiss.IndexFlatL2(d)
assert index.ntotal == 0
index.add(xb)
assert index.ntotal == nb

distances, neighbors = index.search(xq, k)
assert distances.shape == (nq, k), distances.shape
assert neighbors.shape == (nq, k), neighbors.shape
# IndexFlatL2 为精确检索,最近邻应与暴力计算结果一致。
gt = np.argsort(((xq[:, None, :] - xb[None, :, :]) ** 2).sum(-1), axis=1)[:, :k]
assert (neighbors == gt).all(), "nearest neighbour mismatch"
print("faiss IndexFlatL2 add/search nearest-neighbour ok")
PY
}

# 更完整的功能:训练 IVF+PQ 量化索引并检索,校验已训练/已入库且返回合法邻居。
write_ivf_pq_smoke() {
    cat >"$1" <<'PY'
import numpy as np
import faiss

rng = np.random.default_rng(1)
d, nt, nb, nq, k = 64, 4096, 2048, 8, 5
xt = rng.random((nt, d), dtype="float32")
xb = rng.random((nb, d), dtype="float32")
xq = rng.random((nq, d), dtype="float32")

quantizer = faiss.IndexFlatL2(d)
index = faiss.IndexIVFPQ(quantizer, d, 16, 8, 8)
assert not index.is_trained
index.train(xt)
assert index.is_trained

index.add(xb)
assert index.ntotal == nb
index.nprobe = 16
distances, neighbors = index.search(xq, k)
assert distances.shape == (nq, k), distances.shape
assert neighbors.shape == (nq, k), neighbors.shape
assert (neighbors >= 0).all(), "invalid neighbour id"
print("faiss IndexIVFPQ train/add/search ok")
PY
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" f_status="$4" f_detail="$5" i_status="$6" i_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_F_STATUS="${f_status}" FUNC_F_DETAIL="${f_detail}" \
        FUNC_I_STATUS="${i_status}" FUNC_I_DETAIL="${i_detail}" \
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
        {"name": "index-flat-l2", "status": env("FUNC_F_STATUS"), "detail": env("FUNC_F_DETAIL")},
        {"name": "index-ivf-pq", "status": env("FUNC_I_STATUS"), "detail": env("FUNC_I_DETAIL")},
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
    local flat_status="failed" flat_detail=""
    local ivf_status="failed" ivf_detail=""

    # 1) 版本
    actual="$(read_installed_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="faiss ${actual}"
    else
        version_detail="faiss ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi
    printf 'version: %s (%s)\n' "${version_status}" "${version_detail}" >>"${log_file}"

    # 2) 基础 smoke:IndexFlatL2
    if write_flat_l2_smoke "${TMPDIR}/faiss_flat.py" && \
        "${PYBIN}" "${TMPDIR}/faiss_flat.py" >>"${log_file}" 2>&1; then
        flat_status="passed"
        flat_detail="IndexFlatL2 add/search returned correct nearest neighbours"
    else
        flat_detail="IndexFlatL2 smoke failed; see functional_test.log"
    fi
    printf 'index-flat-l2: %s (%s)\n' "${flat_status}" "${flat_detail}" >>"${log_file}"

    # 3) 训练型索引:IVF+PQ
    if write_ivf_pq_smoke "${TMPDIR}/faiss_ivfpq.py" && \
        "${PYBIN}" "${TMPDIR}/faiss_ivfpq.py" >>"${log_file}" 2>&1; then
        ivf_status="passed"
        ivf_detail="IndexIVFPQ train/add/search ok"
    else
        ivf_detail="IndexIVFPQ smoke failed; see functional_test.log"
    fi
    printf 'index-ivf-pq: %s (%s)\n' "${ivf_status}" "${ivf_detail}" >>"${log_file}"

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${flat_status}" != "passed" || "${ivf_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${flat_status}" "${flat_detail}" \
        "${ivf_status}" "${ivf_detail}"
    log "checks: version=${version_status} index-flat-l2=${flat_status} index-ivf-pq=${ivf_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    # build 模式删除本次任务创建的 venv;prebuilt 模式不产生额外安装。
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        log "prebuilt mode: nothing installed by this case to clean"
    else
        [[ -d "${VENV_DIR}" ]] && rm -rf -- "${VENV_DIR}"
        [[ -d "${BUILD_DIR}" ]] && rm -rf -- "${BUILD_DIR}"
        log "removed the task-private venv and build directory"
    fi
    [[ -d "${TMPDIR}" ]] && rm -rf -- "${TMPDIR}/faiss_flat.py" "${TMPDIR}/faiss_ivfpq.py"
    log "cleaned faiss task data"
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
Run the faiss functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    faiss version (default: ${SOFTWARE_VERSION})
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