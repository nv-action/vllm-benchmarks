# 🎯 Qwen2.5-7B-Instruct
  <div>
    <strong>vLLM Version:</strong>
    vLLM:
    <a href="https://github.com/vllm-project/vllm/commit/b6553be1bc75f046b00046a4ad7576364d03c835"
       target="_blank" rel="noopener noreferrer">
       0.9.1@b6553be
    </a>
    ,
    vLLM Ascend:
    <a href="https://github.com/vllm-project/vllm-ascend/commit/db2f630aebb0cad44f9705ac028993233a00c82e"
       target="_blank" rel="noopener noreferrer">
      v0.9.0rc2@db2f630
    </a>
  <br>
  </div>
  <div>
      <strong>vLLM Engine:</strong> V0 <br>
  </div>
  <div>
      <strong>Software Environment:</strong> CANN: 8.1.RC1, PyTorch: 2.5.1, torch-npu: 2.5.1.post1.dev20250528 <br>
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
  export MODEL_ARGS='pretrained=Qwen/Qwen2.5-7B-Instruct,max_model_len=4096,dtype=auto,tensor_parallel_size=2,gpu_memory_utilization=0.6'
lm_eval --model vllm --modlel_args $MODEL_ARGS --tasks ceval-valid_computer_network \ 
--apply_chat_template --fewshot_as_multiturn --num_fewshot 5 --batch_size 1
  ```
  </div>
  <div>&nbsp;</div>
  
| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | 0.6842 | ± 0.1096 |
<details>
<summary>ceval-valid_computer_network details</summary>

| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | 0.6842 | ± 0.1096 |
</details>
