import argparse

from numpy import full

from data_processor.stability.utils import read_from_json, calculate_volatility_metrics
from data_processor.es_om.handler import DataHandler


parser = argparse.ArgumentParser()
parser.add_argument('--res_folder', type=str, required=True)
parser.add_argument('--commit_id', type=str, required=True)
handler = DataHandler()
handler.index_name = 'vllm_volatility_metrics'

if __name__ == '__main__':
    args = parser.parse_args()
    full_data = read_from_json(args.res_folder)
    volatility = calculate_volatility_metrics(full_data)
    handler.add_single_data(args.commit_id, volatility)