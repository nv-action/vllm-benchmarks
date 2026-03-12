# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-12
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: 5282c7d4d0d1487eb283f09d322b0140dea5a968
# Total commits: 50

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. Platform Interface - New Method for Block Size
FILE: vllm/platforms/interface.py
CHANGE: Added new method `update_block_size_for_backend()` that ensures block_size is compatible with attention backend. This is called during platform config verification.
IMPACT: vLLM Ascend's platform implementation (vllm_ascend/platform.py) may need to implement or override this method for proper attention backend compatibility.
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 2. Model Runner - Import Changes and Renames
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE:
  - Added import for `KVConnectorOutput`
  - `CudaGraphManager` renamed to `ModelCudaGraphManager`
  - `get_cudagraph_and_dp_padding` and `make_num_tokens_across_dp` imports changed
  - New imports: `BatchExecutionDescriptor`, `ModelCudaGraphManager`, `get_uniform_token_count`
  - Rejection sampling import changed: `rejection_sample` function replaced with `RejectionSampler` class
IMPACT: vLLM Ascend's model runner implementations that extend or reference vLLM's model runner need to update imports and class names.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/*.py
  - Any files that import from vllm.v1.worker.gpu.model_runner

### 3. Rejection Sampler Refactoring
FILE: vllm/v1/worker/gpu/spec_decode/
CHANGE:
  - New file: `rejection_sampler.py` with `RejectionSampler` class
  - Old: `rejection_sample.py` with standalone `rejection_sample` function
  - Adds strict rejection sampling support
IMPACT: Any vLLM Ascend code that uses rejection sampling needs to update to use the new class-based API.
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/*.py

### 4. Platform Custom Collectives Method
FILE: vllm/platforms/interface.py
CHANGE: Added new method `use_custom_op_collectives()` that returns False by default. Platforms can opt-in to use torch.ops.vllm.* custom ops for collectives.
IMPACT: vLLM Ascend may need to override this if using custom collective ops.
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. Speculative Config - NGram GPU Types
FILE: vllm/config/vllm.py
CHANGE: Added `NgramGPUTypes` to speculative config imports and validation. Async scheduling now supports NGram GPU speculative decoding.
IMPACT: If vLLM Ascend has any validation or handling related to speculative decoding methods, it should be updated to include NGram GPU types.
VLLM_ASCEND_FILES:
  - vllm_ascend/config.py
  - vllm_ascend/spec_decode/*.py

### 2. KV Connector CUDA Graph Mode Handling
FILE: vllm/config/vllm.py
CHANGE: Added logic to check if KV connector requires PIECEWISE mode for CUDA graphs. Automatically overrides cudagraph_mode to PIECEWISE if needed.
IMPACT: vLLM Ascend's KV connector implementations should implement the `requires_piecewise_for_cudagraph()` method if they need special CUDA graph handling.
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/*.py

### 3. Platform __getattr__ Fix
FILE: vllm/platforms/interface.py
CHANGE: Fixed `__getattr__` to raise AttributeError for dunder methods instead of returning None, fixing pickle compatibility.
IMPACT: This is a bug fix that should automatically apply to vLLM Ascend's platform class.
VLLM_ASCEND_FILES:
  - None (automatic fix)

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. Attention Backend Block Size Validation
FILE: vllm/config/ (multiple files)
CHANGE: ROCm-specific changes for block size validation in attention backends.
IMPACT: Review if vLLM Ascend's attention backends need similar block size handling.
VLLM_ASCEND_FILES:
  - vllm_ascend/attention/*.py

### 2. Model Runner Data Parallelism
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Added explicit `dp_size` and `dp_rank` attributes to GPUModelRunner.
IMPACT: Review if vLLM Ascend's model runner needs similar data parallelism attribute handling.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/*.py

### 3. Encoder-Decoder Model Support
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Added `is_encoder_decoder` attribute to model runner.
IMPACT: Review if vLLM Ascend needs to handle encoder-decoder models differently.
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/*.py

================================================================================
## P3 - Model Changes
================================================================================

### 1. New Model Support
CHANGE: Various new models and model updates (Gemma, Qwen3, LFM2, etc.)
IMPACT: Minimal impact on vLLM Ascend core functionality.
VLLM_ASCEND_FILES:
  - None (unless specific model support is needed)

### 2. Model Config Changes
CHANGE: Various model config updates for new features and models.
IMPACT: Review if any model configs need updates in vLLM Ascend.
VLLM_ASCEND_FILES:
  - vllm_ascend/model_loader.py (if exists)

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. Documentation Updates
CHANGE: Various documentation and comment updates.
IMPACT: No code changes needed.

### 2. Test File Reorganization
CHANGE: Some test files moved to better organization.
IMPACT: No impact on production code.

### 3. Example File Moves
CHANGE: Examples reorganized under examples/basic/ directory.
IMPACT: No impact on core functionality.

================================================================================
## Files/Directories Renamed
================================================================================

examples/offline_inference/basic/* -> examples/basic/offline_inference/*
examples/online_serving/* -> examples/basic/online_serving/*
tests/entrypoints/openai/test_render.py -> tests/entrypoints/openai/cpu/test_render.py
tests/v1/kv_connector/unit/test_kv_connector_lifecyle.py -> tests/v1/kv_connector/unit/test_kv_connector_lifecycle.py
vllm/transformers_utils/processors/funasr_processor.py -> vllm/transformers_utils/processors/funasr.py

================================================================================
## END OF CHANGES
================================================================================
