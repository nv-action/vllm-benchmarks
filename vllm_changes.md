# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-25
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 14771f715085f5026919d30048329c3e822d4b3c
# Total commits: 50+

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. [V0 Deprecation] Refactor kv cache from list to element
FILE: vllm/model_executor/layers/attention/attention.py
CHANGE: `kv_cache` changed from a list indexed by `virtual_engine` to a single tensor.
IMPACT: Code accessing `attn_layer.kv_cache[virtual_engine]` will fail.
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/attention_v1.py (uses kv_cache[0], kv_cache[1] for key/value - different pattern, likely unaffected)
  - vllm_ascend/attention/mla_v1.py (uses kv_cache[0], kv_cache[1] - different pattern)
  - vllm_ascend/attention/sfa_v1.py (uses kv_cache[0], kv_cache[1] - different pattern)

NOTES: vllm-ascend uses kv_cache as a tuple of (key_cache, value_cache), not as a list
indexed by virtual_engine. This is a different pattern and should remain compatible.

### 2. vllm_is_batch_invariant() replaced with envs.VLLM_BATCH_INVARIANT
FILE: vllm/model_executor/layers/attention/attention.py
CHANGE: `vllm_is_batch_invariant()` function replaced with direct `envs.VLLM_BATCH_INVARIANT` check.
IMPACT: Code using `vllm_is_batch_invariant()` will fail with ImportError.
VLLM_ASCEND_FILES:
  - No direct usage found in vllm_ascend.

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. Platform interface new methods
FILE: vllm/platforms/interface.py
CHANGE: Added new methods to Platform class:
  - `is_zen_cpu()` - returns False by default
  - `update_block_size_for_backend()` - class method to update block_size based on attention backend
  - `support_deep_gemm()` - returns False by default
  - `use_custom_op_collectives()` - returns False by default
  - `__getattr__` now raises AttributeError for dunder methods
IMPACT: No breaking changes - all new methods have default implementations.
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py (NPUPlatform inherits from Platform)

### 2. MoE layer signature changes
FILE: vllm/model_executor/layers/fused_moe/layer.py
CHANGE: `maybe_roundup_hidden_size()` signature changed - removed `is_mxfp4_quant` parameter.
IMPACT: Code calling with `is_mxfp4_quant` argument will fail.
VLLM_ASCEND_FILES:
  - No direct usage found in vllm_ascend.

### 3. SpeculativeConfig new fields
FILE: vllm/config/speculative.py
CHANGE: Added new fields:
  - `rejection_sample_method` - "strict" or "probabilistic"
  - `extract_hidden_states` method type
  - `NgramGPUTypes` literal type
IMPACT: New configuration options available.
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py (handles speculative_config)

### 4. Quantization method changes
FILE: vllm/model_executor/layers/quantization/__init__.py
CHANGE: Removed `ptpc_fp8`, added `mxfp8` quantization method.
IMPACT: Models using `ptpc_fp8` will fail.
VLLM_ASCEND_FILES:
  - vllm_ascend/quantization/ (may need updates if using these methods)

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. CudaGraphManager refactoring
FILE: vllm/v1/worker/gpu/cudagraph_utils.py
CHANGE: Significant refactoring:
  - `CudaGraphManager` renamed to `ModelCudaGraphManager`
  - New `BatchExecutionDescriptor` dataclass
  - New `get_uniform_token_count()` function
IMPACT: May affect graph capture logic.
VLLM_ASCEND_FILES:
  - vllm_ascend/compilation/acl_graph.py (ACLGraphWrapper)
  - vllm_ascend/worker/model_runner_v1.py

### 2. Model runner refactoring
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Major refactoring:
  - New `RejectionSampler` class replacing `rejection_sample` module
  - New `KVConnectorOutput` import
  - Changes to initialization pattern
IMPACT: May affect vllm-ascend model runner.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner_v1.py

### 3. Attention layer changes
FILE: vllm/model_executor/layers/attention/attention.py
CHANGE: `block_size` parameter removed from attention init.
IMPACT: Code passing `block_size` to attention init will fail.
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/*.py

================================================================================
## P3 - Model Changes
================================================================================

### 1. [XPU] support MLA model on Intel GPU
FILE: vllm/v1/attention/backends/mla/xpu_mla_sparse.py (new)
CHANGE: New XPU MLA sparse attention backend.

### 2. Granite 4.0 1B speech model added
FILE: vllm/model_executor/models/
CHANGE: New model architecture supported.

### 3. Nemotron H Puzzle support
FILE: vllm/config/speculative.py
CHANGE: Added `nemotron_h_puzzle` to supported model types.

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. FlashAttention allow custom mask mod
FILE: vllm/v1/attention/backends/flex_attention.py
CHANGE: Added support for custom mask mod.

### 2. Schedule requests based on full ISL
FILE: vllm/config/scheduler.py, vllm/v1/core/
CHANGE: New option to schedule requests based on full ISL.

### 3. Limit thinking tokens (hard limit)
FILE: vllm/config/reasoning.py
CHANGE: New feature to limit thinking tokens.

### 4. Auto-enable prefetch on NFS with RAM guard
FILE: vllm/v1/worker/
CHANGE: Performance optimization for NFS.

================================================================================
## Files/Directories Renamed
================================================================================

- `vllm/model_executor/layers/quantization/ptpc_fp8.py` -> removed
- `vllm/model_executor/layers/quantization/mxfp8.py` -> new file

================================================================================
## END OF CHANGES
================================================================================