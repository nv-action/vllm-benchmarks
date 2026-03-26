# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-26
# Old commit: 35141a7eeda941a60ad5a4956670c60fd5a77029 (v0.18.0 tag)
# New commit: 71161e8b63f9534d6ac5e098a4874621164d1f1e
# Total commits: 110

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. vllm_is_batch_invariant() function removed
FILE: vllm/model_executor/layers/batch_invariant.py
CHANGE: The `vllm_is_batch_invariant()` function has been removed. The `VLLM_BATCH_INVARIANT` variable and function have been moved to `vllm/envs.py` and should be accessed as `envs.VLLM_BATCH_INVARIANT`.
IMPACT: All code importing and calling `vllm_is_batch_invariant()` will fail with ImportError or AttributeError.
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py
  - vllm_ascend/batch_invariant.py
  - vllm_ascend/sample/sampler.py
  - vllm_ascend/utils.py

### 2. Attention kv_cache structure changed from list to tensor
FILE: vllm/model_executor/layers/attention/attention.py, vllm/model_executor/layers/attention/mla_attention.py
CHANGE: `self.kv_cache` changed from a list of tensors (one per PP stage) to a single tensor. Previously: `self.kv_cache = [torch.tensor([]) for _ in range(pipeline_parallel_size)]`. Now: `self.kv_cache = torch.tensor([])`.
IMPACT: Code accessing `self.kv_cache[0]` or `self.kv_cache[virtual_engine]` will fail with IndexError or incorrect behavior.
VLLM_ASCEND_FILES:
  - vllm_ascend/patch/worker/patch_qwen3_5.py
  - vllm_ascend/patch/worker/patch_qwen3_next.py
  - vllm_ascend/patch/worker/patch_qwen3_next_mtp.py
  - vllm_ascend/ops/mla.py

### 3. vllm_is_batch_invariant() usage in distributed code
FILE: vllm/distributed/device_communicators/all_reduce_utils.py, vllm/distributed/device_communicators/symm_mem.py
CHANGE: Internal vLLM code updated to use `envs.VLLM_BATCH_INVARIANT` instead of `vllm_is_batch_invariant()`.
IMPACT: vllm-ascend code importing from these modules may need updates if they reference the old function.
VLLM_ASCEND_FILES:
  - N/A (indirect impact only)

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. Input types renamed in vllm/inputs module
FILE: vllm/inputs/__init__.py
CHANGE: Major renaming of input types:
  - `ProcessorInputs` -> `EngineInput`
  - `DecoderOnlyInputs` -> `DecoderOnlyEngineInput`
  - `EncoderDecoderInputs` -> `EncoderDecoderInput`
  - `TokenInputs` -> `TokensInput`
  - `EmbedsInputs` -> `EmbedsInput`
  - `SingletonInputs` -> `SingletonInput`
IMPACT: Code using old type names will fail with ImportError or AttributeError.
VLLM_ASCEND_FILES:
  - None (not used in vllm-ascend)

### 2. Platform interface validate_request signature change
FILE: vllm/platforms/interface.py
CHANGE: `validate_request` method parameter type changed from `ProcessorInputs` to `EngineInput`.
IMPACT: Platform implementations may need type annotation updates.
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py (if overriding validate_request)

### 3. ROCm platform changes - VLLM_ROCM_CUSTOM_PAGED_ATTN removed
FILE: vllm/platforms/rocm.py
CHANGE: Environment variable `VLLM_ROCM_CUSTOM_PAGED_ATTN` removed from envs.py and platform code.
IMPACT: ROCm-specific configurations, no impact on Ascend.
VLLM_ASCEND_FILES:
  - N/A

### 4. SpeculativeConfig gains moe_backend field
FILE: vllm/config/speculative.py
CHANGE: Added `moe_backend: MoEBackend | None` field to SpeculativeConfig.
IMPACT: Speculative decoding with MoE draft models may need additional configuration.
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/ (if applicable)

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. MoE layer and fused_moe changes
FILE: vllm/model_executor/layers/fused_moe/*.py
CHANGE: Multiple changes to MoE kernels including:
  - FP8 dtype now uses `current_platform.fp8_dtype()` instead of hardcoded `torch.float8_e4m3fn`
  - Addition of `get_fp8_min_max` utility
  - Changes to Triton kernels for precision improvements
IMPACT: MoE implementations in vllm-ascend may need similar updates.
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/moe.py
  - vllm_ascend/ops/fused_moe/

### 2. KV cache refactoring for V0 deprecation
FILE: vllm/v1/worker/block_table.py
CHANGE: Refactoring of KV cache from list to element, block table API changes.
IMPACT: Worker and model runner code may need updates.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner_v1.py
  - vllm_ascend/worker/v2/model_runner.py

### 3. EPLB changes
FILE: vllm/distributed/eplb/*.py
CHANGE: EPLB async worker changes, removed main waits for slow EPLB.
IMPACT: EPLB implementations in vllm-ascend may need alignment.
VLLM_ASCEND_FILES:
  - vllm_ascend/eplb/

### 4. KV Offload refactoring
FILE: vllm/v1/worker/*.py
CHANGE: CPU offloading refactored with pluggable CachePolicy, removed Backend abstraction.
IMPACT: KV offload implementations may need updates.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/

================================================================================
## P3 - Model Changes
================================================================================

### 1. Multiple model updates
FILES: vllm/model_executor/models/*.py
CHANGE: Various model updates including:
  - aria.py, audioflamingo3.py, aya_vision.py, bagel.py, bailing_moe_linear.py
  - bee.py, blip2.py, chameleon.py, clip.py, cohere2_vision.py, cohere_asr.py
  - deepseek_ocr.py, deepseek_ocr2.py, deepseek_v2.py, deepseek_vl2.py
  - And many more multimodal and vision models
IMPACT: Model-specific code may need updates for new architectures.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner_v1.py (model registration)

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. VLLM_BATCH_INVARIANT moved to envs.py
FILE: vllm/envs.py
CHANGE: `VLLM_BATCH_INVARIANT` environment variable handling moved from batch_invariant.py to envs.py.
IMPACT: No functional change, just code organization.
VLLM_ASCEND_FILES:
  - All files using batch invariant mode

### 2. CPU platform improvements
FILE: vllm/platforms/cpu.py
CHANGE: Added tcmalloc preloading for Linux ARM and X86, improved CPU architecture detection.
IMPACT: CPU-specific, no impact on Ascend.
VLLM_ASCEND_FILES:
  - N/A

### 3. XPU platform changes
FILE: vllm/platforms/xpu.py
CHANGE: Removed MLA-specific code, added torch.xpu.empty_cache() call.
IMPACT: XPU-specific, no impact on Ascend.
VLLM_ASCEND_FILES:
  - N/A

================================================================================
## Files/Directories Renamed
================================================================================

None in this update.

================================================================================
## END OF CHANGES
================================================================================