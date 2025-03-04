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
            'name': key,
            'variance': float(variance),
            'std_dev': float(std_dev),
            'range': float(data_range),
            'cv': float(cv),
        }
    return volatility_metrics


def get_dataframe(data_map: dict):
    return pd.DataFrame(data_map).T



def read_from_json(folder_path: Union[Path, str]):
    key_to_collect = [item.value for item in FullDataServing]
    collected_data = {key: [] for key in key_to_collect}

    if not os.path.exists(folder_path):
        raise FileNotFoundError(f"{folder_path} do not exist")
    
    for filename in os.listdir(folder_path):
        if filename.endswith('.json'):
            file_path = os.path.join(folder_path, filename)
            try:
                with open(file_path, 'r') as f:
                    data = json.load(f)
                    for key in collected_data:
                        collected_data[key].append(data.get(key, None))
            except Exception as e:
                print(f"error while reading from {file_path}")
    return collected_data





