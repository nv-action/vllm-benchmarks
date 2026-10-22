#!/usr/bin/env bash
# pytorch 测试脚本:功能判定 + 收尾。
# 判定:版本校验 + 矩阵乘/autograd 反向回环 smoke + 小型 nn 训练回环 smoke。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 基础 smoke:矩阵乘 + autograd 反向,校验梯度与解析解一致。
write_matmul_autograd_smoke() {
    cat >"$1" <<'PY'
import torch

torch.manual_seed(0)
a = torch.randn(256, 256, requires_grad=True)
b = torch.randn(256, 256)
c = a @ b
assert c.shape == (256, 256)
assert torch.isfinite(c).all()

loss = (c ** 2).sum()
loss.backward()
assert a.grad is not None and a.grad.shape == a.shape
assert torch.isfinite(a.grad).all()
# d(sum((a@b)^2))/da = 2 * (a@b) @ b^T
expected = 2 * (a.detach() @ b) @ b.t()
assert torch.allclose(a.grad, expected, rtol=1e-3, atol=1e-2), "autograd gradient mismatch"
print("torch matmul + autograd backward ok")
PY
}

# 更完整的功能:小型全连接网络用 SGD 训练,校验损失下降。
write_nn_training_smoke() {
    cat >"$1" <<'PY'
import torch
from torch import nn

torch.manual_seed(0)
model = nn.Sequential(nn.Linear(32, 64), nn.ReLU(), nn.Linear(64, 1))
optimizer = torch.optim.SGD(model.parameters(), lr=0.05)
loss_fn = nn.MSELoss()

x = torch.randn(256, 32)
w = torch.randn(32, 1)
y = x @ w + 0.01 * torch.randn(256, 1)

first = last = None
for step in range(200):
    optimizer.zero_grad()
    loss = loss_fn(model(x), y)
    loss.backward()
    optimizer.step()
    if step == 0:
        first = loss.item()
    last = loss.item()

assert last < first, f"training did not reduce loss: {first} -> {last}"
print(f"torch nn training loop ok (loss {first:.4f} -> {last:.4f})")
PY
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" m_status="$4" m_detail="$5" n_status="$6" n_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_M_STATUS="${m_status}" FUNC_M_DETAIL="${m_detail}" \
        FUNC_N_STATUS="${n_status}" FUNC_N_DETAIL="${n_detail}" \
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
        {"name": "matmul-autograd", "status": env("FUNC_M_STATUS"), "detail": env("FUNC_M_DETAIL")},
        {"name": "nn-training-loop", "status": env("FUNC_N_STATUS"), "detail": env("FUNC_N_DETAIL")},
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
    local matmul_status="failed" matmul_detail=""
    local nn_status="failed" nn_detail=""

    # 1) 版本
    actual="$(read_installed_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="torch ${actual}"
    else
        version_detail="torch ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi
    printf 'version: %s (%s)\n' "${version_status}" "${version_detail}" >>"${log_file}"

    # 2) 基础 smoke:矩阵乘 + autograd
    if write_matmul_autograd_smoke "${TMPDIR}/torch_matmul.py" && \
        "${PYBIN}" "${TMPDIR}/torch_matmul.py" >>"${log_file}" 2>&1; then
        matmul_status="passed"
        matmul_detail="matmul forward/backward matched the analytic gradient"
    else
        matmul_detail="matmul/autograd smoke failed; see functional_test.log"
    fi
    printf 'matmul-autograd: %s (%s)\n' "${matmul_status}" "${matmul_detail}" >>"${log_file}"

    # 3) 小型 nn 训练回环
    if write_nn_training_smoke "${TMPDIR}/torch_nn.py" && \
        "${PYBIN}" "${TMPDIR}/torch_nn.py" >>"${log_file}" 2>&1; then
        nn_status="passed"
        nn_detail="nn training loop reduced the loss"
    else
        nn_detail="nn training smoke failed; see functional_test.log"
    fi
    printf 'nn-training-loop: %s (%s)\n' "${nn_status}" "${nn_detail}" >>"${log_file}"

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${matmul_status}" != "passed" || "${nn_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${matmul_status}" "${matmul_detail}" \
        "${nn_status}" "${nn_detail}"
    log "checks: version=${version_status} matmul-autograd=${matmul_status} nn-training-loop=${nn_status}"

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
    [[ -d "${TMPDIR}" ]] && rm -rf -- "${TMPDIR}/torch_matmul.py" "${TMPDIR}/torch_nn.py"
    log "cleaned pytorch task data"
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
Run the pytorch functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    PyTorch version (default: ${SOFTWARE_VERSION})
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