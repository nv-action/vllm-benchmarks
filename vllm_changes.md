# vLLM Changes Relevant to vLLM Ascend
# Generated: 2026-03-26
# Old commit: 4034c3d32e30d01639459edd3ab486f56993876d (v0.16.0 tag)
# New commit: e2db2b42347ae27da3083c530c71b251861ed220
# Total commits: 880

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### 1. CudaGraphManager Refactored to ModelCudaGraphManager
FILE: vllm/v1/worker/gpu/cudagraph_utils.py
CHANGE: CudaGraphManager class significantly refactored:
  - Renamed to ModelCudaGraphManager
  - Constructor signature changed from (vllm_config, use_aux_hidden_state_outputs, device) to (vllm_config, device, cudagraph_mode, decode_query_len)
  - New BatchExecutionDescriptor dataclass added
  - New get_uniform_token_count() function added
  - Graph storage changed from dict[int, CUDAGraph] to dict[BatchExecutionDescriptor, CUDAGraph]
IMPACT: vllm-ascend's AclGraphManager inherits from CudaGraphManager and will break at runtime
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/v2/aclgraph_utils.py

### 2. Inputs Module Refactored - ProcessorInputs renamed to EngineInput
FILE: vllm/inputs/__init__.py
CHANGE: Major refactoring of inputs module:
  - ProcessorInputs renamed to EngineInput
  - DecoderOnlyInputs renamed to DecoderOnlyEngineInput
  - EncoderDecoderInputs renamed to EncoderDecoderInput
  - TokenInputs renamed to TokensInput
  - EmbedsInputs renamed to EmbedsInput
  - SingletonInputs renamed to SingletonInput
  - token_inputs() renamed to tokens_input()
  - embeds_inputs() renamed to embeds_input()
IMPACT: Platform.validate_request() signature changed - any code using ProcessorInputs will fail
VLLM_ASCEND_FILES:
  - Check for ProcessorInputs usage across codebase (grep found none, but verify)

### 3. Platform Interface Extended with New Methods
FILE: vllm/platforms/interface.py
CHANGE: New abstract methods added to Platform class:
  - is_zen_cpu() - returns False by default
  - update_block_size_for_backend() - adjusts block_size based on attention backend
  - support_deep_gemm() - returns False by default
  - use_custom_op_collectives() - returns False by default
  - validate_request() signature changed: processed_inputs type changed from ProcessorInputs to EngineInput
  - __getattr__ now properly handles dunder methods for pickle compatibility
IMPACT: NPUPlatform may need to implement these new methods for compatibility
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 4. Rejection Sampling Refactored to Class-based Approach
FILE: vllm/v1/worker/gpu/spec_decode/rejection_sample.py (DELETED)
FILE: vllm/v1/worker/gpu/spec_decode/rejection_sampler.py (NEW)
CHANGE: rejection_sample() function replaced with RejectionSampler class:
  - Old: rejection_sample() standalone function
  - New: RejectionSampler class with __init__(sampler, num_speculative_steps, use_strict_rejection_sampling)
  - Supports both "strict" and "probabilistic" rejection sampling methods
IMPACT: vllm-ascend has its own rejection_sampler.py but imports from vllm
VLLM_ASCEND_FILES:
  - vllm_ascend/sample/rejection_sampler.py
  - vllm_ascend/worker/model_runner_v1.py

### 5. GPUModelRunner Constructor and Initialization Changes
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: Major refactoring of GPUModelRunner:
  - Constructor no longer uses pp_size local variable, uses self.use_pp directly
  - New intermediate_tensors member for non-first PP ranks
  - sampler, rejection_sampler, prompt_logprobs_worker, structured_outputs_worker are now Optional
  - New EPLBController integration
  - load_model() now accepts load_dummy_weights parameter
  - New decode_query_len attribute
IMPACT: NPUModelRunner inherits from GPUModelRunner and may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/worker/model_runner_v1.py

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================

### 1. EPLB Controller Added to Model Runner
FILE: vllm/v1/worker/gpu/eplb_utils.py (NEW)
CHANGE: New EPLBController class added:
  - Manages EPLB state for load balancing
  - step_eplb_after() decorator for stepping after model runner methods
  - Integration with model loading and speculator registration
IMPACT: vllm-ascend has its own EPLB implementation that may need alignment
VLLM_ASCEND_FILES:
  - vllm_ascend/eplb/

### 2. Speculative Decoding Config Extended
FILE: vllm/config/speculative.py
CHANGE: New config options added:
  - moe_backend: MoEBackend | None - allows draft model to use different MoE backend
  - rejection_sample_method: "strict" | "probabilistic" - controls rejection sampling
  - New spec methods: "extract_hidden_states", "ngram_gpu"
  - New EagleModelTypes: "extract_hidden_states"
IMPACT: Speculative decoding configurations may need updates
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

### 3. Speculative Decoding Backend Selection
FILE: vllm/v1/worker/gpu/spec_decode/__init__.py
CHANGE: Support for per-draft-model MoE backend via --speculative-config
IMPACT: May affect how spec decode backends are selected
VLLM_ASCEND_FILES:
  - vllm_ascend/spec_decode/

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================

### 1. DeepGEMM Platform Support
FILE: vllm/platforms/interface.py, vllm/platforms/cuda.py
CHANGE: New support_deep_gemm() platform method
IMPACT: Platforms need to declare DeepGEMM support
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 2. Custom Op Collectives Platform Method
FILE: vllm/platforms/interface.py
CHANGE: New use_custom_op_collectives() platform method
IMPACT: Platforms can opt-in to use torch.ops.vllm.* custom ops for collectives
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 3. Block Size Backend Adjustment
FILE: vllm/platforms/interface.py
CHANGE: New update_block_size_for_backend() method adjusts block_size based on attention backend
IMPACT: Platforms can customize block size based on their attention backend
VLLM_ASCEND_FILES:
  - vllm_ascend/platform.py

### 4. KV Connector Output Type
FILE: vllm/v1/worker/gpu/model_runner.py
CHANGE: KVConnectorOutput added to imports
IMPACT: May affect KV transfer functionality
VLLM_ASCEND_FILES:
  - vllm_ascend/distributed/kv_transfer/

================================================================================
## P3 - Model Changes
================================================================================

### 1. Various Model Fixes
- Cohere-Transcribe enabled
- Plamo 2/3 & LFM2 for Transformers v5 fixes
- Minimax m2.5 nvfp4 kv scales weight loading fix
- Qwen3.5-FP8 weight loading error fix on TPU

================================================================================
## P4 - Configuration/Minor Changes
================================================================================

### 1. Transformers v5 Compatibility
FILE: Multiple
CHANGE: Various fixes for Transformers v5 compatibility

### 2. ROCm Improvements
FILE: Multiple ROCm-specific files
CHANGE: MLA kernel from AITER, rope+kvcache fusion conditions

### 3. XPU Support
FILE: vllm/platforms/xpu.py, vllm/_xpu_ops.py
CHANGE: MLA model support on Intel GPU, memory usage alignment with CUDA

================================================================================
## Files/Directories Renamed
================================================================================

### Input Types Renamed
- vllm/inputs/data.py -> vllm/inputs/engine.py (module renamed)
- vllm/inputs/llm.py (new module for LLM-specific input types)

### Speculative Decoding
- vllm/v1/worker/gpu/spec_decode/rejection_sample.py -> rejection_sampler.py

================================================================================
## END OF CHANGES
================================================================================