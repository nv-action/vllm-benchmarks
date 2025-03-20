from mmengine.config import read_base
with read_base():
    from opencompass.configs.datasets.ceval.ceval_gen import ceval_datasets
    from opencompass.configs.datasets.gsm8k.gsm8k_gen import gsm8k_datasets
    from .Qwen2_7B import models as ch_llm_model
datasets = ceval_datasets + gsm8k_datasets
models = ch_llm_model