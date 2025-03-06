from handler import DataHandler
from common import VLLM_SCHEMA
from itertools import islice


test_name = 'serving_llama8B_tp1_qps_4'
prefix, model_name = islice(test_name.split('_'), 2)
print(prefix)
print(model_name)