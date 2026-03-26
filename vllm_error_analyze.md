# vLLM-Ascend CI Failure Analysis Report

## Overview

| Item                      | Value                                        |
| :------------------------ | :------------------------------------------- |
| **Run URL**               | N/A (Run ID 23583800497 not found)           |
| **Run Date**              | 2026-03-26                                   |
| **Good Commit (pinned)**  | `35141a7eeda941a60ad5a4956670c60fd5a77029`   |
| **Bad Commit (tested)**   | `71161e8b63f9534d6ac5e098a4874621164d1f1e`   |
| **Total Commits Analyzed**| 110                                          |
| **Distinct Issues Found** | 2 P0 breaking changes + 4 P1 important changes |

## Analysis Summary

The run ID 23583800497 provided in the request does not exist in either the Meihan-chen/vllm-benchmarks or vllm-project/vllm-ascend repositories. This analysis is based on comparing the good and bad commits to identify potential issues.

### Commit 4d747ce8 Already Applied

The commit `4d747ce8d7c9b5a8a7a06f7a8cf90f6bc682aa43` on branch `main2main_auto_2026-03-26_07-52` already contains the necessary adaptations for the vLLM changes.

## Issue Analysis

### Issue 1: vllm_is_batch_invariant() Function Removed

| Item                      | Detail                                   |
| :------------------------ | :--------------------------------------- |
| **Category**              | Code Bug (P0 Breaking Change)            |
| **Error Type**            | ImportError / AttributeError             |
| **Affected Tests**        | Any test using batch invariant mode      |
| **Root Cause Commit**     | Multiple commits in the range            |
| **Changed File**          | `vllm/model_executor/layers/batch_invariant.py` |
| **Impact in vllm-ascend** | `vllm_ascend/ascend_config.py`, `vllm_ascend/batch_invariant.py`, `vllm_ascend/sample/sampler.py`, `vllm_ascend/utils.py` |

**Explanation:** The `vllm_is_batch_invariant()` function was removed from vLLM. The `VLLM_BATCH_INVARIANT` variable and function were moved to `vllm/envs.py` and should be accessed as `envs.VLLM_BATCH_INVARIANT`.

**Fix Applied:** All affected files have been updated to use `envs.VLLM_BATCH_INVARIANT` instead of `vllm_is_batch_invariant()`.

### Issue 2: Attention kv_cache Structure Changed

| Item                      | Detail                                   |
| :------------------------ | :--------------------------------------- |
| **Category**              | Code Bug (P0 Breaking Change)            |
| **Error Type**            | IndexError / TypeError                   |
| **Affected Tests**        | Any test using attention/kv_cache        |
| **Root Cause Commit**     | Multiple commits in the range            |
| **Changed File**          | `vllm/model_executor/layers/attention/attention.py` |
| **Impact in vllm-ascend** | `vllm_ascend/ops/mla.py`, `vllm_ascend/patch/worker/patch_qwen3_*.py` |

**Explanation:** `self.kv_cache` changed from a list of tensors (one per PP stage) to a single tensor. Previously: `self.kv_cache = [torch.tensor([]) for _ in range(pipeline_parallel_size)]`. Now: `self.kv_cache = torch.tensor([])`.

**Fix Applied:** All affected files have been updated to access `self.kv_cache` directly instead of `self.kv_cache[0]` or `self.kv_cache[virtual_engine]`.

## Summary Table

| # | Error | Category | Upstream Cause | Affected Files | Fix Status |
| :--- | :---- | :------- | :-------------- | :------------- | :--- |
| 1 | `ImportError: cannot import vllm_is_batch_invariant` | P0 Breaking | Function removed | ascend_config.py, batch_invariant.py, sampler.py, utils.py | Fixed |
| 2 | `IndexError` on kv_cache access | P0 Breaking | Structure changed | mla.py, patch_qwen3_*.py | Fixed |

## P1 Changes (No Fix Required)

| Change | Impact | Status |
| :--- | :--- | :--- |
| Input types renamed (`ProcessorInputs` -> `EngineInput`) | Not used in vllm-ascend | No action needed |
| Platform interface validate_request signature change | Not overridden in vllm-ascend | No action needed |
| ROCm platform changes | ROCm-specific | No action needed |
| SpeculativeConfig gains moe_backend field | Not used in vllm-ascend spec_decode | No action needed |

## Commit References Updated

All vLLM commit references have been updated from `35141a7eeda9` to `71161e8b63f9` in:
- `.github/workflows/bot_pr_create.yaml`
- `.github/workflows/pr_test_full.yaml`
- `.github/workflows/pr_test_light.yaml`
- `.github/workflows/dockerfiles/Dockerfile.lint`
- `docs/source/community/versioning_policy.md`

## Recommended Actions

1. **No additional fixes required** - All P0 breaking changes have been addressed in commit 4d747ce8.
2. **Verify E2E tests pass** - Run E2E-Full CI to confirm all adaptations are working correctly.
3. **Monitor for P2 changes** - MoE layer changes and KV cache refactoring may require attention if issues arise.

## Files Changed in 4d747ce8

```
 .github/workflows/bot_pr_create.yaml             |   2 +-
 .github/workflows/dockerfiles/Dockerfile.lint    |   2 +-
 .github/workflows/pr_test_full.yaml              |   2 +-
 .github/workflows/pr_test_light.yaml             |   6 +-
 docs/source/community/versioning_policy.md       |   2 +-
 vllm_ascend/ascend_config.py                     |   4 +-
 vllm_ascend/batch_invariant.py                   |   4 +-
 vllm_ascend/ops/mla.py                           |   2 +-
 vllm_ascend/patch/worker/patch_qwen3_5.py        |   2 +-
 vllm_ascend/patch/worker/patch_qwen3_next.py     |   2 +-
 vllm_ascend/patch/worker/patch_qwen3_next_mtp.py |   3 +-
 vllm_ascend/sample/sampler.py                    |   4 +-
 vllm_ascend/utils.py                             |   4 +-
 vllm_changes.md                                  | 161 +++++++++++++++++++++++
 14 files changed, 180 insertions(+), 20 deletions(-)
```