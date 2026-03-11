# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-11
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 81939e7733642f583d1731e5c9ef69dcd457b5e5
# Total commits: 298

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Platform Interface New Methods
FILE: vllm/platforms/interface.py
CHANGE: Two new abstract methods added to Platform interface:
  - `update_block_size_for_backend(vllm_config)`: Ensure block_size is compatible with attention backend
  - `use_custom_op_collectives()`: Whether platform should use torch.ops.vllm.* custom ops for collectives
IMPACT: vLLM Ascend's AscendPlatform implementation MUST implement these new abstract methods
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 2. Model Runner V1 Major Refactoring
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Major refactoring of CUDA graph management and model execution:
  - CudaGraphManager renamed to ModelCudaGraphManager
  - New imports: BatchExecutionDescriptor, ModelCudaGraphManager, get_uniform_token_count
  - Removed imports: get_cudagraph_and_dp_padding, make_num_tokens_across_dp (moved to dp_utils)
  - Constructor signature changed: now takes compilation_config.cudagraph_mode and decode_query_len
  - New method: profile_cudagraph_memory() (returns 0, marked as TBD)
  - execute_model_state changed from tuple to ExecuteModelState NamedTuple
  - Speculator execution changed: speculator.propose() now called with explicit parameters
  - torch.cuda.synchronize() replaced with torch.accelerator.synchronize()
IMPACT: Any vLLM Ascend code that extends or interacts with GPUModelRunner must be updated
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner.py (if exists)
  - Any custom model runner implementations

### 3. CUDA Graph Utils Complete Refactoring
FILE: vllm/v1/worker/gpu/cudagraph_utils.py
CHANGE: Complete architectural refactoring:
  - CudaGraphManager renamed to ModelCudaGraphManager
  - New dataclass: BatchExecutionDescriptor(cg_mode, num_tokens, num_reqs, uniform_token_count)
  - New helper functions:
    - _is_compatible(desc, num_reqs, num_tokens, uniform_token_count)
    - get_uniform_token_count(num_reqs, num_tokens, max_query_len)
  - Constructor signature changed: removed use_aux_hidden_state_outputs, added cudagraph_mode and decode_query_len
  - Internal architecture completely changed: _init_candidates(), dispatch() methods
  - Removed: get_cudagraph_sizes(), uniform_decode_cudagraph_sizes
IMPACT: Any code using CudaGraphManager API must be updated to new ModelCudaGraphManager API
VLLM_ASCEND_FILES:
  - Any code importing or using vllm.v1.worker.gpu.cudagraph_utils
  - Custom CUDA graph management code

### 4. DP Utils Refactoring
FILE: vllm/v1/worker/gpu/dp_utils.py
CHANGE: Complete refactoring of DP synchronization:
  - get_batch_metadata_across_dp() removed
  - get_cudagraph_and_dp_padding() removed
  - New function: sync_cudagraph_and_dp_padding(cudagraph_manager, desired_batch_desc, ...)
  - make_num_tokens_across_dp() kept but simplified
  - Return types changed: now returns (BatchExecutionDescriptor, num_tokens_across_dp)
  - Function signature and behavior completely changed
IMPACT: Any code calling DP utilities must be updated to new API
VLLM_ASCEND_FILES:
  - Any code using vllm.v1.worker.gpu.dp_utils
  - Distributed training code with data parallelism

### 5. Speculative Config New Methods
FILE: vllm/config/speculative.py
CHANGE: New speculative decoding methods added:
  - New method type: "extract_hidden_states" (extracts intermediate hidden states)
  - New method type: "ngram_gpu" (GPU-based ngram)
  - New helper method: update_arch_() (updates architecture-related fields)
  - uses_aux_hidden_states logic expanded to support "extract_hidden_states"
  - New method: uses_extract_hidden_states()
  - EagleModelTypes extended to include "extract_hidden_states"
IMPACT: Speculative decoding config handling must support new methods
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/mtp_proposer.py
  - vllm_ascend/spec_decode/eagle_proposer.py
  - Any speculative decoding related code

### 6. API Renames and Removals
CHANGE: Several API renames and removals:
  - should_torch_compile_mm_vit → should_torch_compile_mm_encoder
  - compile_ranges_split_points → compile_ranges_endpoints
  - group_mm_kwargs_by_modality → group_and_batch_mm_kwargs
  - disable_fallback field removed
  - swap_space parameter removed (v0 deprecation)
  - Default ray dependency removed
IMPACT: Any code using these old API names will break
VLLM_ASCEND_FILES:
  - Any code using these renamed APIs
  - Configuration and argument parsing code

### 7. KVConnectorOutput.merge() Method
FILE: vllm/v1/outputs.py
CHANGE: New class method added:
  - KVConnectorOutput.merge(*outputs): Merges multiple KVConnectorOutput objects
  - Helper function: _combine_non_none(f, items)
IMPACT: Code working with KVConnectorOutput may need updating
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/kv_connector/v1/*.py

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. torch.cuda.synchronize() → torch.accelerator.synchronize()
FILE: Multiple files across vllm/
CHANGE: Platform-agnostic synchronization API adopted:
  - torch.cuda.synchronize() replaced with torch.accelerator.synchronize()
  - Part of Hardware API refactor (#36085)
IMPACT: Any platform-specific synchronization code should use new API
VLLM_ASCEND_FILES:
  - Any code using torch.cuda.synchronize()
  - Platform-specific synchronization code

### 2. MoE Layer Weight Scale Handling
FILE: vllm/model_executor/layers/fused_moe/layer.py
CHANGE: Updated weight scale loading for quantization:
  - Added check for BLOCK weight scales
  - ModelOpt MXFP8 MoE uses block scales, not per-tensor scales
  - is_block_weight_scale flag added
IMPACT: MoE quantization code may need review
VLLM_ASCEND_FILES:
  - vllm_ascend/quantization/methods/modelslim/
  - Any custom MoE implementations

### 3. V1 Outputs Enhancements
FILE: vllm/v1/outputs.py
CHANGE: KVConnectorOutput merge functionality added
  - New merge() class method for combining multiple outputs
  - Useful for distributed scenarios
IMPACT: Review for distributed KV transfer optimizations
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/

### 4. Attention Layer Refactor
CHANGE: Attention check_and_update_config refactored (#35122)
  - Reapplied refactor for better attention configuration handling
IMPACT: Custom attention implementations may need review
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. Processor File Renames
CHANGE: Some processor files renamed:
  - funasr_processor.py → funasr.py (moved to processors/ directory)
IMPACT: Import paths may need updating
VLLM_ASCEND_FILES:
  - Any code importing funasr_processor

### 2. Example Directory Restructuring
CHANGE: Examples directory restructured:
  - examples/offline_inference/basic/* → examples/basic/offline_inference/*
IMPACT: Documentation or references to example paths
VLLM_ASCEND_FILES:
  - Documentation files

### 3. Test File Moves
CHANGE: Some test files moved:
  - tests/entrypoints/openai/test_render.py → tests/entrypoints/openai/cpu/test_render.py
  - tests/v1/kv_connector/unit/test_kv_connector_lifecyle.py → test_kv_connector_lifecycle.py (typo fix)
IMPACT: Test imports and references

### 4. Dependency Changes
CHANGE: Ray dependency removed as default requirement
IMPACT: Review if vLLM Ascend requires Ray for specific features

### 5. Various Bug Fixes
CHANGE: Multiple bug fixes across core components:
  - SSM cache blocks zeroing for Mamba/Qwen3.5
  - FunASR model bugfix
  - LFM2 MoE test fixes for Transformers v5
  - Qwen2.5-VL test fixes for Transformers v5
  - Various test improvements
IMPACT: Review if vLLM Ascend has similar issues

================================================================================
## P3 - Model Changes
================================================================================

### 1. New Model Support
CHANGE: New models added:
  - HyperCLOVAX-SEED-Think-32B vision-language model
  - Nemotron v3 reasoning parser
IMPACT: No direct impact on vLLM Ascend unless supporting these models

### 2. Model Optimizations
CHANGE: Various performance improvements:
  - Maxsim computation moved to worker side (2.7% E2E improvement)
  - Sparse MLA CG support fixes
  - ROCm sparse_mla cudagraph enablement
IMPACT: Review for similar optimizations in vLLM Ascend

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. Typo Fixes
CHANGE: Minor typo fixes:
  - homogenous → homogeneous
  - lifecycle typo fix in test file name
IMPACT: None

### 2. Documentation Updates
CHANGE: Documentation improvements:
  - Security guidance expansion for --allowed-media-domains
  - Various README updates
IMPACT: Review vLLM Ascend documentation for consistency

### 3. CI/CD Improvements
CHANGE: Test infrastructure improvements:
  - ROCm test optimizations
  - Async scheduling test improvements
  - Model Runner V2 CI tests added
IMPACT: Review vLLM Ascend CI configuration

================================================================================
## Files/Directories Renamed
================================================================================
R088	examples/offline_inference/basic/README.md → examples/basic/offline_inference/README.md
R100	examples/offline_inference/basic/basic.py → examples/basic/offline_inference/basic.py
R100	examples/offline_inference/basic/chat.py → examples/basic/offline_inference/chat.py
R100	examples/offline_inference/basic/classify.py → examples/basic/offline_inference/classify.py
R085	examples/offline_inference/basic/embed.py → examples/basic/offline_inference/embed.py
R100	examples/offline_inference/basic/generate.py → examples/basic/offline_inference/generate.py
R086	examples/offline_inference/basic/reward.py → examples/basic/offline_inference/reward.py
R100	examples/offline_inference/basic/score.py → examples/basic/offline_inference/score.py
R100	examples/online_serving/openai_chat_completion_client.py → examples/basic/online_serving/openai_chat_completion_client.py
R100	examples/online_serving/openai_completion_client.py → examples/basic/online_serving/openai_completion_client.py
R099	tests/entrypoints/openai/test_render.py → tests/entrypoints/openai/cpu/test_render.py
R100	tests/v1/kv_connector/unit/test_kv_connector_lifecyle.py → tests/v1/kv_connector/unit/test_kv_connector_lifecycle.py
R089	vllm/transformers_utils/processors/funasr_processor.py → vllm/transformers_utils/processors/funasr.py

================================================================================
## Summary of Critical Changes for vLLM Ascend
================================================================================

MUST ADAPT (P0):
1. Implement new Platform interface methods: update_block_size_for_backend(), use_custom_op_collectives()
2. Update Model Runner V1 code for new CUDA graph management API
3. Update CUDA Graph Utils imports and usage (CudaGraphManager → ModelCudaGraphManager)
4. Update DP Utils function calls (sync_cudagraph_and_dp_padding)
5. Support new speculative decoding methods: extract_hidden_states, ngram_gpu
6. Update renamed APIs: should_torch_compile_mm_encoder, compile_ranges_endpoints, etc.
7. Replace torch.cuda.synchronize() with torch.accelerator.synchronize()

SHOULD REVIEW (P1):
1. Review MoE quantization weight scale handling
2. Review KVConnectorOutput.merge() for distributed scenarios
3. Check attention layer configuration changes

================================================================================
## END OF CHANGES
================================================================================
