# Qwen3-8B Accuracy Test
  <div>
    <strong>vLLM version:</strong> vLLM: 0.7.3, vLLM Ascend: v0.7.3rc2 <br>
  </div>
  <div>
      <strong>Software Environment:</strong> CANN: 8.1.RC1, PyTorch: 2.5.1, torch-npu: 2.5.1.dev20250320 <br>
  </div>
  <div>
      <strong>Hardware Environment</strong>: Atlas A2 Series <br>
  </div>
  <div>
      <strong>Datasets</strong>: ceval-valid_computer_network <br>
  </div>
  <div>
      <strong>Command</strong>: 

  ```bash
  export MODEL_AEGS='Qwen/Qwen3-8B, max_model_len=4096,dtype=auto,tensor_parallel_size=2,gpu_memory_utilization=0.6'
lm_eval --model vllm --modlel_args $MODEL_ARGS --tasks ceval-valid_computer_network \ 
--apply_chat_template --fewshot_as_multiturn --num_fewshot 5 --batch_size 1
  ```
  </div>
  <div>&nbsp;</div>
  
| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | ↑ 0.1053 | ± 0.0723 |
<details>
<summary>ceval-valid_computer_network details</summary>

| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | ↑ 0.1053 | ± 0.0723 |
</details>
