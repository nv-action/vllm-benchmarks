from calendar import c
from dataclasses import dataclass, asdict
from pathlib import Path
from sys import flags
import re
from typing import List, Optional, Dict, Union
import os
import json
from datetime import datetime
from argparse import ArgumentParser


cli_parser = ArgumentParser()


@dataclass
class BaseDataEntry:
    commit_id: str
    commit_title: str
    test_name: str
    tp: int
    created_at: Union[str, None]

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class ServingDataEntry(BaseDataEntry):
    mean_ttft_ms: float
    median_ttft_ms: float
    p99_ttft_ms: float
    mean_tpot_ms: float
    p99_tpot_ms: float
    median_tpot_ms: float
    mean_itl_ms: float
    median_itl_ms: float
    p99_itl_ms: float
    request_rate: str


# Throughput
@dataclass
class ThroughputDataEntry(BaseDataEntry):
    requests_per_second: float
    tokens_per_second: float

# Latency
@dataclass
class LatencyDataEntry(BaseDataEntry):
    mean_latency: float
    median_latency: float
    percentile_99: float


def get_project_root() -> Path:
    current_path = Path(__file__).resolve()
    while current_path != current_path.parent:
        if (current_path / '.git').exists() or (current_path / 'setup.py').exists():
            return current_path
        current_path = current_path.parent
    return current_path


def read_from_json(folder_path: Union[str, Path]):
     json_data_list = {}
     for file_name in os.listdir(folder_path):
        if file_name.endswith('json'):
             file_path = os.path.join(folder_path, file_name)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                json_data = json.load(f)
                json_data_list[Path(file_name).stem] = json_data
        except json.JSONDecodeError as e:
            print(f'can not read from json: {file_name}')
     return json_data_list


def convert_s_ms(time_second: float) -> float:
    return round(time_second * 1000, 2)

def extract_tp_value(s):
    match = re.search(r"tp(\d+)", s)
    return int(match.group(1)) if match else None

def data_prc(folder_path: Union[str, Path], commit_id, commit_title, created_at=None) -> Dict[str,List[Union[ServingDataEntry, LatencyDataEntry, ThroughputDataEntry]]]:
    commit_id = commit_id
    commit_title = commit_title
    json_data = read_from_json(folder_path)
    res_instance = {'vllm_benchmark_serving': [], "vllm_benchmark_latency":[], "vllm_benchmark_throughput":[]}
    for test_name, data in json_data.items():
        test_prefix = str.split(test_name, '_')[0]
        tp = extract_tp_value(test_name)
        match test_prefix:
            case 'serving':
                res_instance["vllm_benchmark_serving"].append(ServingDataEntry(
                    commit_id=commit_id,
                    commit_title=commit_title,
                    test_name=test_name,
                    tp=tp,
                    created_at=created_at,
                    request_rate=data['request_rate'],
                    mean_ttft_ms=data['mean_ttft_ms'],
                    median_ttft_ms=data['median_ttft_ms'],
                    p99_ttft_ms=data['p99_ttft_ms'],
                    mean_itl_ms=data['mean_itl_ms'],
                    median_itl_ms=data['median_itl_ms'],
                    p99_itl_ms=data['p99_itl_ms'],
                    mean_tpot_ms=data['mean_tpot_ms'],
                    median_tpot_ms=data['median_tpot_ms'],
                    p99_tpot_ms=data['p99_tpot_ms']
                ))
            case 'latency':
                res_instance["vllm_benchmark_latency"].append(LatencyDataEntry(
                    commit_id=commit_id,
                    commit_title=commit_title,
                    test_name=test_name,
                    tp=tp,
                    created_at=created_at,
                    mean_latency=convert_s_ms(data['avg_latency']),
                    median_latency=convert_s_ms(data['percentiles']['50']),
                    percentile_99=convert_s_ms(data['percentiles']['99']),
                ))
            case 'throughput':
                res_instance["vllm_benchmark_throughput"].append(ThroughputDataEntry(
                    commit_id=commit_id,
                    commit_title=commit_title,
                    test_name=test_name,
                    created_at=created_at,
                    tp=tp,
                    requests_per_second=data['requests_per_second'],
                    tokens_per_second=data['tokens_per_second'],
                ))

    return res_instance
     

def get_all_commit(file_path):
    res = {}
    with open(file_path, 'r') as f:
        for line in f:
            commit= line.strip().split(' ', 1)
            commit_id, commit_title = commit[0], commit[1]
            res[commit_id] = commit_title
    return res
