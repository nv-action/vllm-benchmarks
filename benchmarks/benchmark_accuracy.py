# SPDX-License-Identifier: Apache-2.0
"""Benchmark the accuracy of processing by opencompass"""
import argparse
import json
import subprocess
import time
import os
import pandas as pd
from vllm.utils import FlexibleArgumentParser
import requests

# def wait_for_server(port, timeout=1200):
#     url = f"http://localhost:{port}/v1/completions"
#     headers = {"Content-Type": "application/json"}
#     payload = {"prompt": "Test prompt"}
#     start_time = time.time()
#     while time.time() - start_time < timeout:
#         try:
#             response = requests.post(url, json=payload, headers=headers, timeout=5)
#             if response.status_code == 200:
#                 return True
#         except Exception:
#             pass
#         time.sleep(1)
#     return False


# def start_vllm_backend(model, port, max_model_len):
#     cmd = f"vllm serve {model} --port {port} --max_model_len {max_model_len}"
#     process = subprocess.Popen(cmd, shell=True)
#     if not wait_for_server(port, timeout=1200):
#         process.terminate()
#         raise RuntimeError("vLLM server failed")
#     return process

def run_opencompass_accuracy(config_file):
    cmd = f"python3 run.py {config_file} --debug"
    subprocess.run(cmd, shell=True, check=True, cwd="../../opencompass")


def main(args: argparse.Namespace):
    # vllm_process = start_vllm_backend(args.model, args.port, args.max_model_len)
    run_opencompass_accuracy(args.config_file)
    subdirs = [d for d in os.listdir(args.output_path) if os.path.isdir(os.path.join(args.output_path, d))]
    subdirs.sort(key=lambda d: os.path.getctime(os.path.join(args.output_path, d)))
    latest_subdir = subdirs[-1] if subdirs else None
    result_file = args.output_path + "/" + latest_subdir + "/summary" + "summary" + "_" + latest_subdir + ".csv"
    if not os.path.exists(result_file):
        return
    df = pd.read_csv(result_file)
    fixed_fields = {"dataset", "version", "metric", "mode"}
    df['dataset'] = df['dataset'].str.replace('ceval-', 'ceval_')
    accuracy_columns = [col for col in df.columns if col not in fixed_fields]
    accuracy_column = accuracy_columns[0]
    results = df.set_index("dataset")[accuracy_column].astype(float).to_dict()
    with open(args.output_json, "w") as f:
            json.dump(results, f, indent=4)

if __name__ == '__main__':
    parser = FlexibleArgumentParser(
        description='Test the accuracy of the model')
    parser.add_argument('--output-path', type=str)
    parser.add_argument('--config-file', type=str)
    parser.add_argument(
        '--output-json',
        type=str,
        default=None,
        help='Path to save the accuracy results in JSON format.')
    args = parser.parse_args()
    main(args)
