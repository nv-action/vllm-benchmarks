# Qwen/Qwen3-30B-A3B

**vLLM Version**: vLLM: 0.10.0 ([6d8d0a2](https://github.com/vllm-project/vllm/commit/6d8d0a2)),
**vLLM Ascend Version**: main ([4fcca13](https://github.com/vllm-project/vllm-ascend/commit/4fcca13))  
**Software Environment**: CANN: 8.2.RC1, PyTorch: 2.5.1, torch-npu: 2.5.1.post1.dev20250619  
**Hardware Environment**: Atlas A2 Series  
**Datasets**: gsm8k  
**Parallel Mode**: TP  
**Execution Mode**: ACLGraph  

**Command**:  

```bash
export MODEL_ARGS='pretrained=Qwen/Qwen3-30B-A3B,tensor_parallel_size=2,dtype=auto,trust_remote_code=False,max_model_len=4096,gpu_memory_utilization=0.6,enable_expert_parallel=True'
lm_eval --model vllm --model_args $MODEL_ARGS --tasks gsm8k \
--apply_chat_template False --fewshot_as_multiturn False  --num_fewshot 5  \
--limit None --batch_size auto
```
| Task                  | Metric      | Value     | Stderr |
|-----------------------|-------------|----------:|-------:|
|                   gsm8k | exact_match,strict-match |✅0.8984078847611827 | ± 0.0083 |
|                   gsm8k | exact_match,flexible-extract |✅0.8544351781652767 | ± 0.0097 |
