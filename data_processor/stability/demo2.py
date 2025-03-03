from ast import main
import subprocess

import numpy as np
import pandas as pd

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


if __name__ == '__main__':
    print(get_dataframe(calculate_volatility_metrics(generate_fake_data())))