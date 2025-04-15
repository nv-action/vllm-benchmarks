#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
#

import argparse
import gc
import json
import multiprocessing
import sys
from multiprocessing import Queue

import lm_eval
import torch
from transformers import AutoConfig

# UNIMODAL_TASK = ["gsm8k", "ceval-valid", "mmlu"]
UNIMODAL_TASK = ["ceval-valid_computer_network"]
MULTIMODAL_TASK = ["mmmu_val"]

MODEL_RUN_INFO = {
    "UNIMODAL": (
        "lm_eval --model vllm \n" 
        "--model_args pretrained={model},max_model_len=4096,\n"
        "dtype=auto,tensor_parallel_size={tp},gpu_memory_utilization=0.6 \n"
        "--tasks {datasets} \n"
        "--apply_chat_template \n"
        "--fewshot_as_multiturn \n"
        "--batch_size 1 \n"
        "--num_fewshot 5 "
    ),
    "MULTIMODA": (
        "lm_eval --model vllm-vlm \n"
        "--model_args pretrained={model},max_model_len=8192,\n"
        "dtype=auto,tensor_parallel_size={tp},max_images=2 \n"
        "--tasks {datasets} \n"
        "--apply_chat_template \n"
        "--fewshot_as_multiturn \n"
        "--batch_size 1 "
    ),
}

def is_multimodal(model_name):
    try:
        cfg = AutoConfig.from_pretrained(model_name, trust_remote_code=True)
    except Exception:
        return False
    has_vision = any(
        hasattr(cfg, attr)
        for attr in ["vision_config", "image_config", "multimodal_config"]
    )
    is_clip = getattr(cfg, "model_type", "").lower().find("clip") != -1
    return has_vision or is_clip

def run_accuracy_unimodal(queue, model, dataset, tp):
    try:
        model_args = f"pretrained={model},max_model_len=4096,dtype=auto,tensor_parallel_size={tp},gpu_memory_utilization=0.6"
        results = lm_eval.simple_evaluate(
            model="vllm",
            model_args=model_args,
            tasks=dataset,
            apply_chat_template=True,
            fewshot_as_multiturn=True,
            batch_size=1,
            num_fewshot=5,
        )
        print(f"Success: {model} on {dataset}")
        measured_value = results["results"]
        queue.put(measured_value)
    except Exception as e:
        print(f"Error in run_accuracy_unimodal: {e}")
        queue.put(e)
        sys.exit(1)
    finally:
        torch.npu.empty_cache()
        gc.collect()


def run_accuracy_multimodal(queue, model, dataset, tp):
    try:
        model_args = f"pretrained={model},max_model_len=8192,dtype=auto,tensor_parallel_size={tp},max_images=2,gpu_memory_utilization=0.8"
        results = lm_eval.simple_evaluate(
            model="vllm-vlm",
            model_args=model_args,
            tasks=dataset,
            apply_chat_template=True,
            fewshot_as_multiturn=True,
            batch_size=1,
        )
        print(f"Success: {model} on {dataset}")
        measured_value = results["results"]
        queue.put(measured_value)
    except Exception as e:
        print(f"Error in run_accuracy_multimodal: {e}")
        queue.put(e)
        sys.exit(1)
    finally:
        torch.npu.empty_cache()
        gc.collect()


def generate_md(model_name, tasks_list, args):
    tp = args.runner.split("-")[-1]
    if args.multimodal:
        model_info = MODEL_RUN_INFO["MULTIMODA"]
    else:
        model_info = MODEL_RUN_INFO["UNIMODAL"]

    run_cmd = model_info.format(model=model_name,
                                                datasets=args.datasets,
                                                tp=tp)
    model = model_name.split("/")[1]
    preamble = f"""# {model} Accuracy Test
  <div>
    <strong>vLLM version:</strong> vLLM: {args.vllm_version}, vLLM Ascend: {args.vllm_ascend_version} <br>
  </div>
  <div>
      <strong>Software Environment:</strong> CANN: {args.cann_version}, PyTorch: {args.torch_version}, torch-npu: {args.torch_npu_version} <br>
  </div>
  <div>
      <strong>Hardware Environment</strong>: Atlas A2 Series <br>
  </div>
  <div>
      <strong>Datasets</strong>: {args.datasets} <br>
  </div>
  <div>
      <strong>Command</strong>: 

  ```bash
  {run_cmd}
  ```
  </div>
  <div>&nbsp;</div>
  """

    header = (
        "| Task                  | Filter | n-shot | Metric   | Value   | Stderr |\n"
        "|-----------------------|-------:|-------:|----------|--------:|-------:|"
    )
    rows = []
    rows_sub = []
    for task_dict in tasks_list:
        for key, stats in task_dict.items():
            alias = stats.get("alias", key)
            task_name = alias.strip()
            if "exact_match,flexible-extract" in stats:
                metric_key = "exact_match,flexible-extract"
            else:
                metric_key = None
                for k in stats:
                    if "," in k and not k.startswith("acc_stderr"):
                        metric_key = k
                        break
            if metric_key is None:
                continue
            metric, flt = metric_key.split(",", 1)

            value = stats[metric_key]
            stderr = stats.get(f"{metric}_stderr,{flt}", 0)
            if not args.multimodal:
                n_shot = "5"
            else:
                n_shot = "0"
            row = (f"| {task_name:<37} "
                   f"| {flt:<6} "
                   f"| {n_shot:6} "
                   f"| {metric:<6} "
                   f"| ↑ {value:>5.4f} "
                   f"| ± {stderr:>5.4f} |")
            if not task_name.startswith("-"):
                rows.append(row)
                rows_sub.append("<details>" + "\n" + "<summary>" + task_name +
                                " details" + "</summary>" + "\n" * 2 + header)
            rows_sub.append(row)
        rows_sub.append("</details>")
    md = preamble + "\n" + header + "\n" + "\n".join(rows) + "\n" + "\n".join(
        rows_sub) + "\n"
    print(md)
    return md


def safe_md(args, accuracy):
    data = json.loads(json.dumps(accuracy))
    for model_key, tasks_list in data.items():
        md_content = generate_md(model_key, tasks_list, args)
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(md_content)
        print(f"create Markdown file:{args.output}")


def main(args):
    accuracy = {}
    accuracy[args.model] = []
    tp = args.runner.split("-")[-1]
    result_queue: Queue[float] = multiprocessing.Queue()
    datasets = list(map(str.strip, args.datasets.split(',')))
    for dataset in datasets:
        p = multiprocessing.Process(target=args.func,
                                    args=(result_queue, args.model,
                                            dataset, tp))
        p.start()
        p.join()
        result = result_queue.get()
        print(result)
        accuracy[args.model].append(result)
    print(accuracy)
    safe_md(args, accuracy)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=str, required=True)
    parser.add_argument("--model", type=str, required=True)
    parser.add_argument("--vllm_ascend_version", type=str, required=False)
    parser.add_argument("--torch_version", type=str, required=False)
    parser.add_argument("--torch_npu_version", type=str, required=False)
    parser.add_argument("--vllm_version", type=str, required=False)
    parser.add_argument("--cann_version", type=str, required=False)
    parser.add_argument("--runner", type=str, required=False)
    args = parser.parse_args()
    args.multimodal = is_multimodal(args.model)
    if args.multimodal:
        args.func = run_accuracy_multimodal
        args.datasets = ",".join(MULTIMODAL_TASK)
    else:
        args.func = run_accuracy_unimodal
        args.datasets = ",".join(UNIMODAL_TASK)
    main(args)
