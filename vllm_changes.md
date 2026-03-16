# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-16
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 57a314d1556cdcb17d26e55e324e21b02bdd9399
# Total commits: 469

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Remove Chunking From FusedMoE
COMMIT: 2cdf92228cfcaa7a3829b557bb4656ec2aeaa599
FILE: vllm/model_executor/layers/fused_moe/fused_moe.py
FILE: vllm/envs.py
CHANGE: Completely removed chunking logic from FusedMoE implementation
IMPACT: All chunking-related code removed:
  - Removed VLLM_FUSED_MOE_CHUNK_SIZE environment variable (default was 16*1024)
  - Removed VLLM_ENABLE_FUSED_MOE_ACTIVATION_CHUNKING environment variable (default was True)
  - Removed chunking loop in fused_experts_impl function
  - Removed supports_chunking() method from TritonExperts class
  - Simplified fused_experts_impl to process all tokens in one call
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/fused_moe/fused_moe.py
  - vllm_ascend/ops/fused_moe/moe_mlp.py
  - vllm_ascend/envs.py (if it exists)

### 2. Hardware API Replacement
COMMIT: 53ec16a70
FILE: Multiple files across vllm/
CHANGE: Replace torch.cuda.device_count/current_device/set_device API
IMPACT: Direct torch.cuda API calls replaced with platform-agnostic APIs
VLLM_ASCEND_FILES:
  - Any files in vllm_ascend/ using torch.cuda.device_count()
  - Any files in vllm_ascend/ using torch.cuda.current_device()
  - Any files in vllm_ascend/ using torch.cuda.set_device()

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. FusedMoE Performance Optimization
COMMIT: f91702098
FILE: vllm/model_executor/layers/fused_moe/fused_moe.py
CHANGE: Optimize FusedMoEModularKernel output tensor using torch.empty
IMPACT: Performance improvement for FusedMoE kernels
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/fused_moe/fused_moe.py

### 2. XPU LoRA Support
COMMIT: 82f836d97
FILE: vllm/model_executor/layers/fused_moe/
CHANGE: Support LoRA via torch.compile on XPU platform
IMPACT: New LoRA compilation approach for XPU
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/fused_moe/ (if LoRA is supported)

### 3. Ray DP Platform Fix
COMMIT: 5353c9b01
FILE: vllm/platforms/
CHANGE: Fix Ray DP startup crash
IMPACT: Platform-specific fixes for distributed execution
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 4. KV Buffer Type Default per Platform
COMMIT: f22d6e026
FILE: vllm/distributed/kv_transfer/
CHANGE: Set default kv buffer type for different platform
IMPACT: Platform-specific KV cache buffer defaults
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/

### 5. Remove Dead Code in KV Connector
COMMIT: 35bdca543
FILE: vllm/distributed/kv_transfer/kv_connector/
CHANGE: Remove dead code in KV connector
IMPACT: Cleanup of unused KV connector code
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/kv_connector/

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. Consolidate SupportsEagle
COMMIT: 8b346309a
FILE: vllm/spec_decode/
CHANGE: Refactor to consolidate SupportsEagle checks
IMPACT: Simplified speculative decoding Eagle support detection
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/mtp_proposer.py
  - vllm_ascend/spec_decode/eagle_proposer.py

### 2. Model Runner V2 Changes
COMMITS: 8ccbcda5c, 6e956d9ec
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Remove unused warmup_for_prefill method, Add dummy profile_cudagraph_memory API
IMPACT: Model runner V2 API changes
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/ (if model runner V2 is adapted)

### 3. Spec Decode Avoid Double Call
COMMIT: d0b402974
FILE: vllm/spec_decode/
CHANGE: Avoid double call of Ngram CPU
IMPACT: Bug fix for speculative decoding
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

### 4. MLA Attention Quantization Fix
COMMITS: 367cf5cd3, 6d53efd2a
FILE: vllm/model_executor/layers/attention/
CHANGE: Enable additional dimension for Flashinfer MLA, Fix MLA attention crash with AWQ/GPTQ
IMPACT: MLA attention improvements and bug fixes
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

### 5. Attention Backend Improvements
COMMIT: 82f3f30e2
FILE: vllm/model_executor/layers/attention/
CHANGE: Enable sparse_mla's cudagraph on ROCm platform
IMPACT: CUDA graph support improvements
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

================================================================================
## P3 - Model Changes
================================================================================

### 1. Mistral Common v10
COMMIT: e42b49bd6
FILE: vllm/model_executor/models/
CHANGE: Add support for Mistral common v10
IMPACT: New model architecture support
VLLM_ASCEND_FILES:
  - vllm_ascend/model_loader.py (if model-specific loading is used)

### 2. DeepSeek-V3.2 Tokenizer Fix
COMMIT: 9efc4db96
FILE: vllm/model_executor/models/
CHANGE: Fix DeepSeek-V3.2 tokenizer stripping spaces
IMPACT: Bug fix for DeepSeek model
VLLM_ASCEND_FILES:
  - Model-specific code in vllm_ascend/

### 3. Model Runner V2 XD-RoPE Support
COMMIT: 3ed46f374
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Add Support for XD-RoPE in Model Runner V2
IMPACT: New RoPE variant support
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/ (if V2 is adapted)

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. V1 Pin Memory Removal
COMMIT: a116c9693
FILE: vllm/v1/
CHANGE: Remove pin_memory() in async_copy_to_gpu to fix sporadic stalls
IMPACT: V1 execution improvement
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/ (if V1 is adapted)

### 2. Shutdown Timeout Re-added
COMMIT: 7afe0faab
FILE: vllm/engine/
CHANGE: Re-add shutdown timeout - allowing in-flight requests to finish
IMPACT: Graceful shutdown improvements
VLLM_ASCEND_FILES:
  - Engine code in vllm_ascend/

### 3. KV Buffer Device Default
COMMIT: 5a3f1eb62
FILE: vllm/config/
CHANGE: Set default kv_buffer_device in a better way
IMPACT: KV cache device placement improvements
VLLM_ASCEND_FILES:
  - vllm_ascend/ascend_config.py

### 4. Chat Template Improvements
COMMITS: 5467d137b, 41aa264f, 458c1a4b2
FILE: vllm/entrypoints/openai/chat_completion/
CHANGE: Avoid startup error log for models without chat template, resolve chat template names before kwargs detection, Reduce chat template warmup logging levels
IMPACT: Chat template handling improvements
VLLM_ASCEND_FILES:
  - None (entrypoint changes)

### 5. Audio Dependency Changes
COMMIT: 6590a3ecd
FILE: vllm/dependencies.py
CHANGE: Remove torchcodec from audio dependency
IMPACT: Audio processing dependency changes
VLLM_ASCEND_FILES:
  - setup.py (dependencies)

================================================================================
## Files/Directories Renamed
================================================================================
- No major file renames detected in this commit range

================================================================================
## END OF CHANGES
================================================================================
