# vLLM-Ascend CI Failure Analysis Report

## Overview

| Item                      | Value                      |
| :------------------------ | :------------------------- |
| **Run URL**               | https://github.com/vllm-project/vllm-ascend/actions/runs/23601323578 |
| **Run Date**              | 2026-03-26 |
| **Good Commit (pinned)**  | `35141a7eeda941a60ad5a4956670c60fd5a77029` |
| **Bad Commit (tested)**   | `2e225f7bd23f533e3ffd909fd5596a85f352c518` |
| **Total Failed Jobs**     | 4 |
| **Distinct Issues Found** | 1 code bug |

## Failed Jobs Summary

| Job        | Conclusion | Failed Tests     |
|:---        |:---        |:---              |
| e2e-test / singlecard-full (0) | failure | tests/e2e/singlecard/compile/test_graphex_qknorm_rope_fusion.py |
| e2e-test / singlecard-full (1) | failure | tests/e2e/singlecard/test_auto_fit_max_mode_len.py |
| e2e-test / multicard-2-full (0) | failure | tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2 |
| e2e-test / multicard-4-full (0) | failure | tests/e2e/multicard/4-cards/test_kimi_k2.py |

## Issue Analysis

### Issue 1: Missing vllm_is_batch_invariant function in vllm_ascend

| Item                      | Detail                                   |
| :------------------------ | :--------------------------------------- |
| **Category**              | Code Bug                                 |
| **Error Type**            | ImportError                              |
| **Affected Tests**        | All e2e tests                            |
| **Root Cause Commit**     | `8d203815` — "[main2main] Adapt to vLLM commit 2e225f7bd23f533e3ffd909fd5596a85f352c518" |
| **Changed File**          | `vllm_ascend/batch_invariant.py`         |
| **Impact in vllm-ascend** | `vllm_ascend/batch_invariant.py`         |

**Error Traceback:**
```
vllm_ascend/sample/sampler.py:2: in <module>
    from vllm.model_executor.layers.batch_invariant import vllm_is_batch_invariant
E   ImportError: cannot import name 'vllm_is_batch_invariant' from 'vllm.model_executor.layers.batch_invariant'
```

**Explanation:**
The vLLM upstream removed the `vllm_is_batch_invariant()` function from `vllm/model_executor/layers/batch_invariant.py`. The main2main adaptation commit correctly updated the code to use `envs_vllm.VLLM_BATCH_INVARIANT` directly, but it also removed the backward-compatible `vllm_is_batch_invariant()` wrapper function from `vllm_ascend/batch_invariant.py`. This function is still needed because:
1. Test files import it from `vllm_ascend.batch_invariant`
2. It provides a convenient API for checking batch invariant mode

**Fix Applied:**
Added the `vllm_is_batch_invariant()` function back to `vllm_ascend/batch_invariant.py` as a backward-compatible wrapper that returns `envs_vllm.VLLM_BATCH_INVARIANT`.

## Summary Table

| #    | Error | Category | Upstream Commit | Affected Tests | Fix  |
| :--- | :---- | :------- | :-------------- | :------------- | :--- |
| 1    | ImportError: cannot import name 'vllm_is_batch_invariant' | Code Bug | 2e225f7bd23f533e3ffd909fd5596a85f352c518 | All e2e tests | Re-add vllm_is_batch_invariant() wrapper function |

## Recommended Actions

1. ✅ Added `vllm_is_batch_invariant()` function back to `vllm_ascend/batch_invariant.py`