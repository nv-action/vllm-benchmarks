# vLLM-Ascend CI Failure Analysis Report

## Overview

| Item                      | Value                                        |
| :------------------------ | :------------------------------------------- |
| **Run URL**               | https://github.com/vllm-project/vllm-ascend/actions/runs/23585442443 |
| **Run Date**              | 2026-03-26                                   |
| **Good Commit (pinned)**  | `71161e8b63f9534d6ac5e098a4874621164d1f1e`   |
| **Bad Commit (tested)**   | Same as pinned (no upstream change detected) |
| **Total Failed Jobs**     | 4 / 8                                        |
| **Distinct Issues Found** | 1 code bug + 0 env flakes                    |

## Failed Jobs Summary

| Job                     | Conclusion | Failed Tests                          |
|:---                     |:---        |:---                                   |
| multicard-4-full (0)    | failure    | All tests (ImportError during import) |
| singlecard-full (0)     | failure    | All tests (ImportError during import) |
| multicard-2-full (0)    | failure    | All tests (ImportError during import) |
| singlecard-full (1)     | failure    | All tests (ImportError during import) |

## Issue Analysis

### Issue 1: Missing `vllm_is_batch_invariant` Function

| Item                      | Detail                                                        |
| :------------------------ | :------------------------------------------------------------ |
| **Category**              | Code Bug                                                      |
| **Error Type**            | `ImportError`                                                 |
| **Affected Tests**        | All E2E tests importing from `vllm_ascend.batch_invariant`    |
| **Root Cause Commit**     | Removal of `vllm_is_batch_invariant()` from vLLM upstream     |
| **Changed File**          | `vllm/model_executor/layers/batch_invariant.py` (vLLM)        |
| **Impact in vllm-ascend** | `vllm_ascend/batch_invariant.py` needs to add the function    |

**Error Traceback:**
```
ImportError while loading conftest '/__w/vllm-ascend/vllm-ascend/tests/e2e/conftest.py'.
E   ImportError: cannot import name 'vllm_is_batch_invariant' from 'vllm.model_executor.layers.batch_invariant'
```

**Explanation:**
In vLLM upstream, the function `vllm_is_batch_invariant()` was removed from `vllm/model_executor/layers/batch_invariant.py`. It was replaced with a direct access to `envs.VLLM_BATCH_INVARIANT` boolean variable.

The vllm-ascend codebase has tests that import `vllm_is_batch_invariant` from `vllm_ascend.batch_invariant`, but this function was never added to the vllm_ascend batch_invariant module. The vllm_ascend batch_invariant.py already uses `envs.VLLM_BATCH_INVARIANT` internally but didn't export a convenience function.

**Fix Applied:**
Added the following function to `vllm_ascend/batch_invariant.py`:
```python
def vllm_is_batch_invariant() -> bool:
    """Check if batch invariant mode is enabled.

    This function provides backward compatibility for code that was using
    the now-removed vllm.model_executor.layers.batch_invariant.vllm_is_batch_invariant
    function from upstream vLLM.
    """
    return envs.VLLM_BATCH_INVARIANT
```

## Summary Table

| #    | Error                                                       | Category   | Upstream Commit | Affected Tests  | Fix                                     |
| :--- | :---------------------------------------------------------- | :--------- | :-------------- | :-------------- | :-------------------------------------- |
| 1    | `ImportError: cannot import name 'vllm_is_batch_invariant'` | Code Bug   | Function removed in vLLM | All E2E tests | Added function to vllm_ascend/batch_invariant.py |

## Recommended Actions

1. ~~Add `vllm_is_batch_invariant()` function to `vllm_ascend/batch_invariant.py`~~ DONE
2. Run tests to verify the fix