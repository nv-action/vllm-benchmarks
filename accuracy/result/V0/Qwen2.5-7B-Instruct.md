<div>
    <strong>Software Environment:</strong> <br>
    vllm: 0.7.3+empty <br> 
    vllm-ascend: v0.7.3rc2 <br>
    torch: 2.5.1 <br> 
    torch_npu: 2.5.1.dev20250320 <br> 
    cann: 8.0.0 <br>
</div>
<div>
    <strong>Hardware Environment</strong>: Atlas A2 Series. <br>
</div>
<div>
    <strong>Datasets</strong>: ceval-valid_computer_network. <br>
</div>
<div>
    <strong>Run Command</strong>: 
<code>lm_eval --model vllm 
--model_args pretrained=Qwen/Qwen2.5-7B-Instruct,max_model_len=4096,
dtype=auto,tensor_parallel_size=2,gpu_memory_utilization=0.6 
--tasks ceval-valid_computer_network 
--apply_chat_template 
--fewshot_as_multiturn 
--batch_size 1 
--num_fewshot 5 
</code>
</div>
<div>&nbsp;</div>

| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
<details>
<summary>ceval-valid_computer_network</summary>

| Task                  | Filter | n-shot | Metric   | Value   | Stderr |
|-----------------------|-------:|-------:|----------|--------:|-------:|
| ceval-valid_computer_network          | none   | 5      | acc    | ↑ 0.6842 | ± 0.1096 |
</details>
