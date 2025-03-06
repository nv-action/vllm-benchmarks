import argparse
from typing import Union, Dict, List
import os

from handler import DataHandler
from data import data_prc, ServingDataEntry, LatencyDataEntry, ThroughputDataEntry

parser = argparse.ArgumentParser(description='add commit msg to es')

parser.add_argument('--commit_id', type=str, required=True)
parser.add_argument('--commit_title', type=str, required=True)
parser.add_argument('--commit_time', type=str, required=True)


def send_data(data_instance: Dict[str, List[Union[ServingDataEntry, LatencyDataEntry, ThroughputDataEntry]]]):
    datahandler =  DataHandler()
    for index_name, data_list in data_instance.items():
        datahandler.index_name = index_name
        for data in data_list:
            insert_id = "_".join(
                filter(None, [data.commit_id, str(getattr(data, "request_rate", "")), getattr(data, "model_name", "")])
            )
            datahandler.add_single_data(insert_id, data.to_dict())


def get_abs_dir():
    base_dir = os.path.dirname(__file__)
    res_dir = os.path.join(base_dir, '../benchmarks/results')
    res_dir = os.path.abspath(res_dir)
    return res_dir


if __name__ == '__main__':
    args = parser.parse_args()
    data_instance = data_prc(get_abs_dir(), args.commit_id, args.commit_title, args.commit_time)
    send_data(data_instance)
    