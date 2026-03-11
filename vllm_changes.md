# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-11
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: f4ae58b38b8ab1d36707344518d699e9019201cc
# Total commits: 321

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Remove swap_space parameter
FILE: vllm/config/cache.py, vllm/config/vllm.py, vllm/engine/arg_utils.py
CHANGE: Removed unused swap_space parameter from CacheConfig and arg_utils
IMPACT: swap_space parameter is no longer supported in vLLM config
VLLM_ASCEND_FILES:
  - tests/ut/core/test_scheduler_dynamic_batch.py
  - tests/e2e/conftest.py
  - benchmarks/tests/serving-tests.json

### 2. Remove disable_fallback field
FILE: vllm/config/structured_outputs.py, vllm/sampling_params.py
CHANGE: Removed unused disable_fallback field
IMPACT: disable_fallback field is no longer available in sampling params
VLLM_ASCEND_FILES:
  - (No usage found in vLLM Ascend code)

### 3. Platform Interface - New Methods
FILE: vllm/platforms/interface.py
CHANGE: Added new platform methods that must be implemented:
  - update_block_size_for_backend(cls, vllm_config) -> None
  - use_custom_op_collectives(cls) -> bool
IMPACT: Ascend platform should implement these methods for compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. Worker/Model Runner Changes
FILE: vllm/v1/worker/gpu/model_runner.py, vllm/v1/worker/gpu_worker.py
CHANGE: Significant refactoring in model_runner (293 insertions, 161 deletions)
  - Updated model execution flow
  - Changes in input processing
  - Enhanced CUDA graph handling
IMPACT: Ascend-specific model runner may need corresponding updates
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/
  - vllm_ascend/v1/worker/

### 2. Attention Backend Changes
FILE: vllm/v1/attention/backends/*
CHANGE: Multiple attention backend updates including:
  - MLA backend improvements
  - New block_size detection logic
  - Enhanced CUDA graph support for sparse attention
IMPACT: Ascend attention implementation may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

### 3. Config Changes
FILE: vllm/config/attention.py, vllm/config/compilation.py
CHANGE:
  - Added --attention-backend auto option
  - Updated compilation config handling
  - Enhanced structured outputs config
IMPACT: May affect Ascend config processing
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. Speculative Decoding Updates
FILE: vllm/v1/spec_decode/
CHANGE: Added hidden states extraction system and EAGLE improvements
IMPACT: Spec decode implementation may need review
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

### 2. Distributed Communication
FILE: vllm/distributed/
CHANGE:
  - Added All-to-All communication backend for DCP
  - Enhanced KV transfer connectors
  - Improved device communicators
IMPACT: Ascend distributed components may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/

### 3. MoE Layer Changes
FILE: vllm/model_executor/layers/fused_moe/
CHANGE: Extensive MoE refactoring including:
  - New prepare_finalize directory structure
  - Enhanced expert implementations
  - Improved router logic
IMPACT: Ascend MoE implementation may need review
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/ (MoE-related ops)

================================================================================
## P3 - Model Changes
================================================================================

### 1. New Models Added
FILE: vllm/model_executor/models/
CHANGE: Added support for new models:
  - kimi_audio.py
  - olmo_hybrid.py
  - fireredasr2.py
  - sarvam.py
  - hyperclovax_vision_v2.py
  - extract_hidden_states.py
IMPACT: New model support (no impact on existing Ascend models)

### 2. Model Updates
CHANGE: Updates to existing models:
  - Gemma2 (removed unused config field)
  - Qwen3 variants
  - DeepSeek V3.2
  - Various vision models
IMPACT: Model-specific updates

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. Build System
FILE: Various
CHANGE:
  - Updated mypy version
  - Enhanced CI configurations
  - Updated dependencies
IMPACT: May affect build/testing

### 2. Documentation
CHANGE: Documentation updates and improvements
IMPACT: No code changes

================================================================================
## Files/Directories Renamed
================================================================================

R089	vllm/transformers_utils/processors/funasr_processor.py -> vllm/transformers_utils/processors/funasr.py

D	vllm/grpc/__init__.py
D	vllm/grpc/compile_protos.py
D	vllm/grpc/vllm_engine.proto

A	vllm/model_executor/models/extract_hidden_states.py
A	vllm/model_executor/layers/fused_moe/experts/__init__.py
A	vllm/model_executor/layers/fused_moe/experts/trtllm_fp8_moe.py
A	vllm/model_executor/layers/fused_moe/experts/trtllm_nvfp4_moe.py
A	vllm/model_executor/layers/fused_moe/prepare_finalize/__init__.py
A	vllm/model_executor/layers/fused_moe/prepare_finalize/naive_dp_ep.py
A	vllm/model_executor/layers/fused_moe/prepare_finalize/no_dp_ep.py
A	vllm/v1/kv_offload/reuse_manager.py
A	vllm/v1/pool/late_interaction.py
A	vllm/v1/worker/gpu/pool/late_interaction_runner.py
A	vllm/distributed/kv_transfer/kv_connector/v1/example_hidden_states_connector.py
A	vllm/entrypoints/openai/realtime/metrics.py
A	vllm/entrypoints/cli/launch.py

================================================================================
## END OF CHANGES
================================================================================
