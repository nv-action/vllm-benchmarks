from ast import parse
import subprocess
import os
from typing import Union
from pathlib import Path
import argparse

from data_processor.es_om import data
from data_processor.stability.utils import get_git_root, calculate_volatility_metrics, get_dataframe

PROJECT_ROOT = get_git_root()

parser = argparse.ArgumentParser()
parser.add_argument("--res_path", type=str, required=True)


def collect_data(file_path: Union[str, Path], res_map: dict):
    result_dir = os.path.join(PROJECT_ROOT, 'benchmarks/results')
    if os.path.isdir(result_dir):
        benchmark_res = data.read_from_json(file_path)
        for _, result in benchmark_res.items():
            res_map['mean_ttft_ms'].append(result['mean_ttft_ms'])
            res_map['median_ttft_ms'].append(result['median_ttft_ms'])
            res_map['p99_ttft_ms'].append(result['p99_ttft_ms'])
            res_map['mean_itl_ms'].append(result['mean_itl_ms'])
            res_map['median_itl_ms'].append(result['median_itl_ms'])
            res_map['p99_itl_ms'].append(result['p99_itl_ms'])
    return res_map


def run_benchmarks():
    benchmark_script = os.path.join(PROJECT_ROOT, '.elastic/nightly-benchmarks/scripts/run-performance-benchmarks.sh')
    res_map = {'mean_ttft_ms':[], 'median_ttft_ms':[], 'p99_ttft_ms':[], 'mean_itl_ms': [], 'median_itl_ms': [], 'p99_itl_ms': []}
    for i in range(10):
        res_dir = os.path.join(PROJECT_ROOT, f'benchmarks/results/res_{i}')
        
        try:
            result = subprocess.run(['bash', benchmark_script, res_dir], check=True, capture_output=True)
            collect_data(res_dir, res_map)
        except subprocess.CalledProcessError as e:
            print(f"Benchmark {i} failed with error: {e.stderr.decode()}") 
    return res_map

if __name__ == '__main__':
    run_benchmarks()
    data_map =  collect_data('/root/wl/oldfiles/vllm-project/vllm-benchmarks/benchmarks/results')
    res =  calculate_volatility_metrics(data_map)
    print(get_dataframe(res))