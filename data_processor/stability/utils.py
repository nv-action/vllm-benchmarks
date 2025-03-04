import subprocess
import json
import os
from typing import Union, List, Dict
from pathlib import Path

import numpy as np
import pandas as pd

from data_processor.stability.common import FullDataServing

def get_git_root():
    try:
        root = subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], stderr=subprocess.STDOUT)
        return root.decode('utf-8').strip()
    except subprocess.CalledProcessError:
        return None

def generate_fake_data(num_samples=10):
    fake_data = {
        'mean_ttft_ms': np.random.normal(loc=171.4, scale=5.0, size=num_samples).tolist(),
        'median_ttft_ms': np.random.normal(loc=172.3, scale=5.0, size=num_samples).tolist(),
        'p99_ttft_ms': np.random.normal(loc=213.2, scale=10.0, size=num_samples).tolist(),
        'mean_itl_ms': np.random.normal(loc=65.1, scale=3.0, size=num_samples).tolist(),
        'median_itl_ms': np.random.normal(loc=60.8, scale=3.0, size=num_samples).tolist(),
        'p99_itl_ms': np.random.normal(loc=126.4, scale=8.0, size=num_samples).tolist()
    }
    return fake_data



def calculate_volatility_metrics(data_dict):
    volatility_metrics = {}
    
    for key, value in data_dict.items():
        values = np.array(value)
        
        variance = np.var(values)

        std_dev = np.std(values)
        
        data_range = np.max(values) - np.min(values)
        
        cv = std_dev / np.mean(values) if np.mean(values) != 0 else 0
        
        volatility_metrics[key] = {
            'variance': variance,
            'std_dev': std_dev,
            'range': data_range,
            'cv': cv,
        }
    
    return volatility_metrics


def get_dataframe(data_map: dict):
    return pd.DataFrame(data_map).T



def read_from_json(file_path: Union[Path, str]):
    with open(file_path, 'r') as f:
        data = json.load(f)
    return data


res_folder = '/root/wl/oldfiles/vllm-project/vllm-benchmarks/benchmarks/tem_res/serving_llama8B_tp1_1.json'

if __name__ == '__main__':
    data = read_from_json(res_folder)
    print(data.get(FullDataServing.MEAN_TTFT_MS, None))