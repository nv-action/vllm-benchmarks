# Qwen3-8B-Base
**vLLM Version**: vLLM: 0.9.2 ([a5dd03c](https://github.com/vllm-project/vllm/commit/a5dd03c)), vLLM Ascend: v0.9.2rc1 ([b5b7e0e](https://github.com/vllm-project/vllm-ascend/commit/b5b7e0e))  
**Software Environment**: CANN: 8.1.RC1, PyTorch: 2.5.1, torch-npu: 2.5.1.post1.dev20250619  
**Hardware Environment**: Atlas A2 Series  
**Datasets**: gsm8k  
**Parallel Mode**: TP  
**Execution Mode**: ACLGraph  
**Command**:  
```bash
export MODEL_ARGS='pretrained=Qwen/Qwen3-8B-Base,max_model_len=4096,dtype=auto,tensor_parallel_size=2,gpu_memory_utilization=0.6'
lm_eval --model vllm --model_args $MODEL_ARGS --tasks gsm8k \ 
--apply_chat_template --fewshot_as_multiturn --num_fewshot 5 --batch_size 1
```
  
| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| gsm8k                                 | flexible-extract | 5      | exact_match | ✅0.8302 | ± 0.0103 |
<details>
<summary>gsm8k details</summary>

| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| gsm8k                                 | flexible-extract | 5      | exact_match | ✅0.8302 | ± 0.0103 |
</details>
