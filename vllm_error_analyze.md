# vLLM-Ascend CI Failure Analysis Report

## Overview

| Item                      | Value                                                      |
| :------------------------ | :--------------------------------------------------------- |
| **Run URL**               | Not accessible (Run ID 22943116541 not found)             |
| **Run Date**              | 2026-03-11                                                 |
| **Analysis Method**       | Manual diff analysis between commits                      |
| **Good Commit (pinned)**  | `4034c3d32e30d01639459edd3ab486f56993876d`                 |
| **Bad Commit (tested)**   | `a40ee486f273eaaa885dafd0526f42f3a5b960c9`                 |
| **Total Commits**         | 19 commits between good and bad                           |
| **Distinct Issues Found** | 3 critical breaking changes                               |

## Analysis Summary

**Note**: CI logs were not accessible for run ID 22943116541. This analysis is based on manual examination of the vLLM upstream diff between the good and bad commits, focusing on breaking changes that commonly affect vllm-ascend.

## Issue Analysis

### Issue 1: get_attn_backend() Signature Change

| Item                      | Detail                                                    |
| :------------------------ | :-------------------------------------------------------- |
| **Category**              | Code Bug - Method Signature Change                        |
| **Error Type**            | TypeError - unexpected keyword argument                   |
| **Affected Files**        | vllm_ascend/worker/model_runner_v1.py                     |
| **Root Cause Commit**     | `a40ee486f` — "[Bugfix] Add Multiple of 16 block_size..." (#35923) |
| **Changed File**          | vllm/model_executor/layers/attention/                     |
| **Impact in vllm-ascend** | vllm_ascend/worker/model_runner_v1.py:276                 |

**Error Traceback (Expected):**
```
TypeError: get_attn_backend() got an unexpected keyword argument 'block_size'
```

**Explanation:**
The upstream vLLM removed the `block_size` parameter from `get_attn_backend()` function. The block size is now managed through the platform's `update_block_size_for_backend()` method. All attention backends now retrieve block size from their KV cache specification instead of receiving it as a parameter.

**Current vllm-ascend code (BROKEN):**
```python
# vllm_ascend/worker/model_runner_v1.py:276
self.attn_backend = get_attn_backend(
    0,  # num_heads
    self.dtype,
    None,  # kv_cache_dtype
    self.block_size,  # ❌ This parameter no longer exists
    use_mla=self.model_config.use_mla,
)
```

**Fix Suggestion:**
Remove the `block_size` parameter from the call:
```python
self.attn_backend = get_attn_backend(
    0,  # num_heads
    self.dtype,
    None,  # kv_cache_dtype
    # block_size removed - now managed by platform
    use_mla=self.model_config.use_mla,
)
```

---

### Issue 2: CacheConfig Structure Reorganization

| Item                      | Detail                                                    |
| :------------------------ | :-------------------------------------------------------- |
| **Category**              | Code Bug - Config Attribute Changes                       |
| **Error Type**            | AttributeError, KeyError                                  |
| **Affected Files**        | Multiple files accessing CacheConfig                      |
| **Root Cause Commit**     | `a40ee486f` — CacheConfig reorganization                  |
| **Changed File**          | vllm/config/cache.py                                      |
| **Impact in vllm-ascend** | Files accessing removed CacheConfig fields                |

**Explanation:**
Upstream vLLM removed several deprecated fields from CacheConfig:
- `swap_space` - removed
- `cpu_offload_gb` - removed (deprecated)
- `cpu_offload_params` - removed (deprecated)
- `block_size` type changed from `Literal[1, 8, 16, 32, 64, 128, 256]` to `int`
- Added `DEFAULT_BLOCK_SIZE: ClassVar[int] = 16`
- Added `user_specified_block_size: bool` field

**Affected vllm-ascend code may need to:**
1. Remove any references to `cache_config.swap_space`
2. Remove any references to `cache_config.cpu_offload_gb` and `cache_config.cpu_offload_params`
3. Update type checks for `cache_config.block_size` (no longer a Literal type)
4. Use `CacheConfig.DEFAULT_BLOCK_SIZE` instead of hardcoded `16`

**Fix Suggestion:**
Search for and remove usages of deprecated fields:
```bash
grep -r "swap_space\|cpu_offload_gb\|cpu_offload_params" vllm_ascend/
```

---

### Issue 3: GPUModelRunner Breaking Changes

| Item                      | Detail                                                    |
| :------------------------ | :-------------------------------------------------------- |
| **Category**              | Code Bug - Class Constructor/Method Changes               |
| **Error Type**            | TypeError, AttributeError                                 |
| **Affected Files**        | vllm_ascend/worker/model_runner_v1.py, vllm_ascend/worker/v2/model_runner.py |
| **Root Cause Commit**     | `a40ee486f` — Model runner refactoring                    |
| **Changed File**          | vllm/v1/worker/gpu/model_runner.py                        |
| **Impact in vllm-ascend** | Model runner subclasses may need updates                  |

**Explanation:**
Upstream GPUModelRunner has significant changes:
1. **CudaGraphManager renamed to ModelCudaGraphManager** with different constructor signature
2. **Added data parallelism support**: `dp_size`, `dp_rank` properties
3. **ExecuteModelState type**: Changed from `tuple | None` to `ExecuteModelState | None`
4. **Speculator handling**: Changed from `prepare_communication_buffer_for_model(self.speculator)` to `prepare_communication_buffer_for_model(self.speculator.model)`
5. **New imports**: `KVConnectorOutput`, `BatchExecutionDescriptor`, `ModelCudaGraphManager`, `get_uniform_token_count`

**Fix Suggestion:**
Review vllm-ascend model runner implementations and update:
- Constructor signature compatibility
- CUDAGraph manager initialization
- Speculator communication buffer setup
- Import statements for new types

---

## Platform Interface Changes (Already Implemented ✅)

The following platform interface additions are **already implemented** in vllm-ascend:

1. **`update_block_size_for_backend(vllm_config)`** - Implemented at `vllm_ascend/platform.py:796`
2. **`use_custom_op_collectives()`** - Implemented at `vllm_ascend/platform.py:820`

No action needed for these methods.

---

## Summary Table

| #    | Error                                              | Category       | Upstream Commit | Affected Tests                           | Fix Priority |
| :--- | :------------------------------------------------- | :------------- | :-------------- | :--------------------------------------- | :----------- |
| 1    | get_attn_backend() block_size parameter            | Method Signature | a40ee486f      | All attention-dependent tests             | **CRITICAL** |
| 2    | CacheConfig deprecated fields removed               | Config Attributes | a40ee486f      | Tests using swap_space or cpu_offload     | **HIGH**     |
| 3    | GPUModelRunner constructor and method changes      | Class Changes  | a40ee486f      | V1 model runner tests                     | **HIGH**     |

---

## Recommended Actions

### Immediate (Phase 2 - Critical Fixes)

1. **Fix get_attn_backend() call in model_runner_v1.py**
   - Remove `block_size` parameter from `get_attn_backend()` call
   - File: `vllm_ascend/worker/model_runner_v1.py:276`
   - Use version guard if needed for backward compatibility

2. **Search and fix deprecated CacheConfig usage**
   ```bash
   grep -rn "swap_space\|cpu_offload" vllm_ascend/
   ```
   - Remove or guard access to removed fields

3. **Review model runner compatibility**
   - Check `vllm_ascend/worker/model_runner_v1.py` for constructor compatibility
   - Check `vllm_ascend/worker/v2/model_runner.py` for similar issues
   - Update CUDAGraph manager initialization if needed

### Follow-up (If issues persist)

4. **Run full test suite** to identify additional failures
5. **Check for attention layer subclasses** that may override methods with old signatures
6. **Verify compilation config changes** don't break ACLGraph integration

---

## Upstream Commit Range

**Good commit**: `4034c3d32e30d01639459edd3ab486f56993876d`
**Bad commit**: `a40ee486f273eaaa885dafd0526f42f3a5b960c9`

**Commits in range**:
```
a40ee486f [Bugfix] Add Multiple of 16 block_size to triton fallback on rocm Attention
eac2dc2b4 AITER MLA backend: Avoid CPU sync in _build_decode
d5080aeaa [Refactor] Remove deadcode in Responses API serving
f22d6e026 [Hardware][NIXL] set default kv buffer type
76c6e6da0 [XPU] Support block fp8 moe by fallback to TritonExpert
418465377 feat: add RISC-V support for CPU backend (v2)
4aaaf8c8c feat(spec_decode): fuse EAGLE step slot mapping
... (19 total commits)
```

---

## Notes

- Analysis based on vLLM upstream diff since CI logs were inaccessible
- Focus on breaking changes in critical paths: config, attention, model runner
- Platform interface changes already implemented in vllm-ascend
- Recommend running actual CI tests to validate fixes and identify additional issues
