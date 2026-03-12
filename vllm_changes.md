# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-12
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 262b76a09fafe15cff7642f3eee433fb903cf1d8
# Total commits: 372

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Platform Interface Enhancement
FILE: vllm/vllm/platforms/interface.py
CHANGE: Added new platform methods including `get_stream` and `register_custom_collective_ops`
IMPACT: Platform implementations must implement these new methods
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 2. Speculative Decoding Changes
FILE: vllm/v1/spec_decode/eagle.py
CHANGE: Major refactoring of EAGLE speculative decoding with new hidden states extraction
IMPACT: Existing EAGLE proposer implementations need to be updated
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/eagle_proposer.py

### 3. Attention Backend Updates
FILE: vllm/model_executor/layers/attention/mla_attention.py
CHANGE: Updates to MLA (Multi-head Latent Attention) with new dimension support (320)
IMPACT: Ascend-specific attention backends need to support new MLA dimensions
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. Fused MoE Optimizations
FILE: vllm/model_executor/layers/fused_moe/
CHANGE: Performance optimizations for FusedMoE kernels, new configs for H100 and MI350X
IMPACT: Ascend MoE implementations should review these optimizations
VLLM_ASCEND_FILES:
  - vllm_ascend/ops/moe/

### 2. KV Connector Enhancements
FILE: vllm/distributed/kv_transfer/kv_connector/v1/
CHANGE: Added support for worker->scheduler metadata transfer
IMPACT: Ascend KV connector implementations may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/

### 3. Speculative Decoding - Hidden States Extraction
FILE: vllm/v1/spec_decode/extract_hidden_states.py (NEW)
CHANGE: New module for extracting hidden states in speculative decoding
IMPACT: New feature that may need Ascend-specific adaptations
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

### 4. Distributed Communication
FILE: vllm/distributed/parallel_state.py
CHANGE: Updates to parallel state management for multi-node TP
IMPACT: Ascend distributed implementations should review
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/

### 5. Model Runner V2 Changes
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Various improvements to model runner for CUDA graphs and pipeline parallel
IMPACT: Ascend model runner may need corresponding updates
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. FlashAttention Integration
FILE: vllm/model_executor/layers/attention/
CHANGE: Updates to FlashAttention backend integration
IMPACT: Review Ascend attention backend compatibility

### 2. AllReduce Backend Changes
FILE: vllm/distributed/communication_ops.py
CHANGE: Default backend for Flashinfer AllReduce changed to trtllm
IMPACT: Review Ascend AllReduce implementations

### 3. Triton Kernel Updates
FILE: vllm/model_executor/kernels/
CHANGE: Various Triton kernel optimizations
IMPACT: Review Ascend-specific kernels for compatibility

### 4. Environment Variable Changes
FILE: vllm/envs.py
CHANGE: Added new environment variables for configuration
IMPACT: Review Ascend environment variable handling
VLLM_ASCEND_FILES:
  - vllm_ascend/envs.py

================================================================================
## P3 - Model Changes
================================================================================

### 1. New Model Support
CHANGE: Added support for new models including:
- Qwen3-VL with MRoPE
- FireRedASR2
- OLMo Hybrid
- HyperCLOVAX-SEED-Think-32B
- Various audio models
IMPACT: No immediate action needed unless these models are Ascend targets

### 2. Model Optimizations
CHANGE: Performance improvements for DeepSeek-V3.2, LFM2, and other models
IMPACT: Review if optimizations apply to Ascend implementations

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. Deprecations
FILE: Multiple files
CHANGE: Removal of items deprecated in v0.18
IMPACT: Review and remove deprecated code if present

### 2. Documentation Updates
FILE: docs/
CHANGE: Various documentation improvements
IMPACT: Update Ascend documentation as needed

### 3. Test Infrastructure
FILE: tests/
CHANGE: New tests for attention, spec_decode, and distributed components
IMPACT: Consider adding similar tests for Ascend components

### 4. Dependency Updates
FILE: pyproject.toml, setup.py
CHANGE: Updated dependency versions
IMPACT: Review Ascend dependency compatibility

================================================================================
## Files/Directories Renamed
================================================================================
None significant

================================================================================
## Key Commit Messages for Review
================================================================================

- [Refactor] Remove dead code in KV connector (#36424)
- [Model Runner V2] Remove unused warmup_for_prefill method (#36762)
- [Deprecation][1/2] Remove items deprecated in v0.18 (#36470)
- [Dependency] Remove default ray dependency (#36170)
- [Hardware] Replace torch.cuda.synchronize() api with torch.accelerator.synchronize (#36085)
- [Bugfix] Fix block_size for hybrid model MTP (#36036)
- [Perf] Optimize compute maxsim using batched version, 3.2% E2E throughput improvement (#36710)
- [Attention][Perf] Replace torch.cat with vectorized CUDA kernel MLA query concat (#34917)
- [Attention][Perf] Optimize cp_gather_and_upconvert_fp8_kv_cache (#35290)
- [Perf] Compute maxsim in worker side, reducing redundant copies (#36159)

================================================================================
## END OF CHANGES
================================================================================

Note: This is a summary of the 372 commits between the old and new vLLM versions.
Focus on P0 and P1 changes for immediate adaptation needs.
