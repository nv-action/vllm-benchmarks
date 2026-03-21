# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-17
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 9c7cab5ebb0f8a15e632e7ea2cfeebcca1d3628f
# Total commits: 538

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Platform Interface Changes - New Methods Required
FILE: vllm/platforms/interface.py
CHANGE: Added new abstract methods to Platform interface:
- is_zen_cpu() - Check if platform is Zen CPU
- update_block_size_for_backend() - Ensure block_size is compatible with attention backend
- use_custom_op_collectives() - Whether platform should use torch.ops.vllm.* custom ops for collectives
- Enhanced __getattr__ with pickle support for dunder methods
IMPACT: vllm-ascend's platform.py must implement these new methods to maintain compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 2. MoE Chunking Removal - Major Refactoring
FILE: vllm/model_executor/layers/fused_moe/fused_moe.py
CHANGE: Removed chunking mechanism from FusedMoE (CHUNK_SIZE logic removed).
The VLLM_FUSED_MOE_CHUNK_SIZE environment variable and chunk-based processing loop
have been completely removed. The function now processes all tokens in a single pass.
IMPACT: Any vllm-ascend code that depends on or extends FusedMoE must be updated to
handle the new non-chunked implementation. The chunk_size parameter is no longer used.
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/moe.py (if it extends or wraps FusedMoE)
  - Any MoE-related code in vllm_ascend/

### 3. Custom Operations - New Functional Variants
FILE: vllm/_custom_ops.py
CHANGE: Added functional + out variant for scaled_fp4_quant operation for
torch.compile buffer management. New functions:
- create_fp4_scale_tensor()
- create_fp4_output_tensors()
- _scaled_fp4_quant_fake() and _scaled_fp4_quant_out_fake() (fake implementations)
IMPACT: If vllm-ascend has custom ops registration or extends quantization operations,
it may need to accommodate these new variants
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/ (any custom ops registration files)
  - vllm_ascend/quantization/ (quantization-related code)

### 4. Config Structure Changes - New Fields and Imports
FILE: vllm/config/vllm.py
CHANGE: Added new configuration fields and imports:
- New field: shutdown_timeout (int) - Shutdown grace period for in-flight requests
- New import: NgramGPUTypes from .speculative
- Updated async scheduling checks to include NgramGPUTypes
- Added KV connector check for PIECEWISE CUDA graph mode
- Fixed typo: "User speciied" -> "User specified"
IMPACT: vllm-ascend's ascend_config.py needs to be reviewed for compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 5. Hardware API Changes - torch.cuda API Replacement
FILE: vllm/utils/mem_utils.py, vllm/model_executor/model_loader/base_loader.py
CHANGE: Replaced memory-related torch.cuda APIs with platform-agnostic equivalents:
- torch.cuda.get_device_properties() -> Platform.get_device_properties()
- torch.cuda.mem_get_info() -> Platform.get_mem_info()
- torch.cuda.current_device() -> Platform.current_device
- Related changes across 8 files
IMPACT: Any vllm-ascend code using torch.cuda memory APIs should use Platform APIs instead
VLLM_ASCEND_FILES:
  - Any vllm_ascend code using torch.cuda memory functions
  - vllm_ascend/utils.py

### 6. MoE Layer Refactoring - Function Rename
FILE: vllm/model_executor/layers/fused_moe/fused_moe.py
CHANGE: Renamed FusedMoEPermuteExpertsUnpermute to FusedMoEExpertsModular
IMPACT: Any references to the old class name in vllm-ascend must be updated
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/moe.py
  - Any code extending MoE modular kernels

### 7. Custom Ops Tensor Allocation Changes
FILE: vllm/_custom_ops.py
CHANGE: Modified tensor allocation in rms_norm_dynamic_per_token_quant:
- torch.empty_like(input, dtype=quant_dtype)
- -> torch.empty(input.shape, dtype=quant_dtype, device=input.device)
IMPACT: May affect vllm-ascend custom ops that use similar patterns
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/ (custom ops implementations)

### 8. Model Runner V2 Updates
FILE: vllm/v1/worker/gpu/model_runner.py (multiple commits)
CHANGE: Multiple Model Runner V2 improvements including:
- Added Support for XD-RoPE
- Added probabilistic rejection sampling for spec decoding
- Added WhisperModelState support
- Fixed processed logits in sample()
- Code simplification and cleanup
IMPACT: If vllm-ascend has model_runner_v1.py or v2/model_runner.py, review for compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner_v1.py
  - vllm_ascend/worker/v2/model_runner.py

### 9. Speculative Decoding Enhancements
FILE: vllm/v1/worker/gpu/spec_decode/ (multiple files)
CHANGE: Multiple spec decode improvements:
- Update extract_hidden_states to use deferred kv_connector clear
- Fuse EAGLE step slot mapping and metadata updates
- Avoid double call of Ngram CPU
- Added NgramGPUTypes support
IMPACT: vllm-ascend spec_decode implementations may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

### 10. Distributed System Changes
FILE: vllm/distributed/ (multiple files)
CHANGE: Updates to:
- device_communicators/ (all2all, cuda_communicator, pynccl, etc.)
- kv_transfer/ (connector factory, v1 connectors)
- eplb/ (elastic parameter load balancing)
- Added KVConnector metadata support
IMPACT: vllm-ascend distributed code needs review
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/
  - vllm_ascend/eplb/

### 11. Attention Layer Changes
FILE: vllm/model_executor/layers/attention/ (multiple files)
CHANGE: Updates to various attention implementations:
- MLA attention updates
- Encoder attention changes
- Static sink attention modifications
IMPACT: vllm-ascend attention implementations may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

### 12. Cache and Memory Management
FILE: vllm/config/cache.py, vllm/config/compilation.py
CHANGE: Updated cache configuration and compilation settings including:
- Block size verification moved from cache_config.verify_with_parallel_config
- New Platform.update_block_size_for_backend() integration
- KV connector PIECEWISE CUDA graph mode checks
IMPACT: May affect vllm-ascend cache and memory management
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py
  - vllm_ascend/compilation/

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 13. Compilation Backend Updates
FILE: vllm/compilation/*.py (11 files changed)
CHANGE: Various compilation backend improvements and bug fixes
IMPACT: May affect vllm-ascend compilation/fusion code
VLLM_ASCEND_FILES:
  - vllm_ascend/compilation/

### 14. Quantization Changes
FILE: vllm/model_executor/layers/quantization/
CHANGE: Updated quantization layers and online MXFP8 quantization support
IMPACT: vllm-ascend quantization code should be reviewed
VLLM_ASCEND_FILES:
  - vllm_ascend/quantization/

### 15. Config Updates
FILE: vllm/config/*.py (13 files changed)
CHANGE: Updates to attention, cache, compilation, kv_transfer, load, model,
observability, parallel, profiler, speculative, structured_outputs configs
IMPACT: vllm-ascend config should be reviewed for compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py
  - vllm_ascend/envs.py

================================================================================
## P3 - Model Changes
================================================================================

### 16. New Model Support and Updates
FILE: vllm/model_executor/models/ (extensive changes)
CHANGE: Multiple model updates including:
- Granite4 tool parser
- Qwen3 ViT improvements
- Mistral parser fixes
- EagleMistralLarge3Model fixes
- Cohere Embed v2 API support
- Music Flamingo loading fix
- Various model-specific improvements
IMPACT: May affect vllm-ascend if it has model-specific implementations
VLLM_ASCEND_FILES:
  - Any model-specific code in vllm_ascend/

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 17. Entry Points and API Updates
FILE: vllm/entrypoints/ (extensive changes)
CHANGE: Updates to OpenAI API, Anthropic API, chat completion, responses API
IMPACT: May affect vllm-ascend if it extends entry points
VLLM_ASCEND_FILES:
  - Any entry point extensions

### 18. Benchmark and Test Updates
FILE: vllm/benchmarks/, tests/
CHANGE: Benchmark improvements and test reorganization
IMPACT: Generally not affecting vllm-ascend functionality

### 19. Documentation Updates
FILE: docs/
CHANGE: Documentation improvements and reorganization
IMPACT: vllm-ascend docs may need updates for consistency

================================================================================
## Files/Directories Renamed
================================================================================
R088	examples/offline_inference/basic/README.md -> examples/basic/offline_inference/README.md
R100	examples/offline_inference/basic/basic.py -> examples/basic/offline_inference/basic.py
R100	examples/offline_inference/basic/chat.py -> examples/basic/offline_inference/chat.py
R100	examples/offline_inference/basic/classify.py -> examples/basic/offline_inference/classify.py
R085	examples/offline_inference/basic/embed.py -> examples/basic/offline_inference/embed.py
R100	examples/offline_inference/basic/generate.py -> examples/basic/offline_inference/generate.py
R086	examples/offline_inference/basic/reward.py -> examples/basic/offline_inference/reward.py
R100	examples/offline_inference/basic/score.py -> examples/basic/offline_inference/score.py
R100	examples/online_serving/openai_chat_completion_client.py -> examples/basic/online_serving/openai_chat_completion_client.py
R100	examples/online_serving/openai_completion_client.py -> examples/basic/online_serving/openai_completion_client.py
... (many test file relocations)

================================================================================
## Summary of Critical Changes for vllm-ascend
================================================================================

Priority | Category | Files Affected
---------|----------|---------------
P0 | Platform Interface | vllm_ascend/platform.py must implement new methods
P0 | MoE Refactoring | vllm_ascend/ops/moe.py - remove chunking dependencies
P0 | Custom Ops | vllm_ascend/ops/ - review quantization ops
P0 | Config | vllm_ascend/ascend_config.py - add new fields
P1 | Hardware APIs | Replace torch.cuda with Platform APIs
P1 | Model Runner | vllm_ascend/worker/ - review V2 changes
P1 | Spec Decode | vllm_ascend/spec_decode/ - review updates
P1 | Distributed | vllm_ascend/distributed/ - review KV connector changes
P1 | Attention | vllm_ascend/attention/ - review MLA changes
P2 | Compilation | vllm_ascend/compilation/ - review backend updates
P2 | Quantization | vllm_ascend/quantization/ - review MXFP8 support

================================================================================
## END OF CHANGES
================================================================================
