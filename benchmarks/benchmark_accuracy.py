# SPDX-License-Identifier: Apache-2.0
"""Benchmark the accuracy of processing by opencompass"""
import argparse
import json
import subprocess
import time
import os
import pandas as pd

def start_vllm_backend(model, port):
    cmd = f"vllm server {model} --port {port}"
    process = subprocess.Popen(cmd, shell=True)
    time.sleep(20)
    return process

def run_opencompass_accuracy(config_file):
    cmd = f"python3 run.py {config_file}"
    subprocess.run(cmd, shell=True, check=True, cwd="opencompass")


def main():
    parser = argparse.ArgumentParser(
        description="Use Opencompass to test the accuracy of the vllm "
        "inference backend and upload the results to Elasticsearch.."
    )
    args = parser.parse_args()
    vllm_process = start_vllm_backend(args.model, args.port)
    run_opencompass_accuracy(args.config_file)
    subdirs = [d for d in os.listdir(args.output_path) if os.path.isdir(os.path.join(args.output_path, d))]
    subdirs.sort(key=lambda d: os.path.getctime(os.path.join(args.output_path, d)))
    latest_subdir = subdirs[-1] if subdirs else None
    result_file = args.path + "/" + latest_subdir + "/summary" + "summary" + "_" + latest_subdir + ".csv"
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
    vllm_process.terminate()
    vllm_process.wait()

if __name__ == '__main__':
    main()
