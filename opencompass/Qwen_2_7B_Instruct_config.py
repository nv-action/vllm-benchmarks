from mmengine.config import read_base
from opencompass.models import OpenAISDK

api_meta_template = dict(
    round=[
        dict(role='HUMAN', api_role='HUMAN'),
        dict(role='BOT', api_role='BOT', generate=True),
    ],
    reserved_roles=[dict(role='SYSTEM', api_role='SYSTEM')],
)

models = [
    dict(
        abbr='Qwen2.5-7B-Instruct-vLLM-API',
        type=OpenAISDK,
        key='EMPTY',
        openai_api_base='http://127.0.0.1:8000/v1',
        path='Qwen/Qwen2.5-7B-Instruct', 
        tokenizer_path='Qwen/Qwen2.5-7B-Instruct', 
        rpm_verbose=True, 
        meta_template=api_meta_template, 
        query_per_second=1, 
        max_out_len=1024,
        max_seq_len=4096,
        temperature=0.01,
        batch_size=16, 
        retry=3, 
    )
]
with read_base():
    from opencompass.configs.datasets.ceval.ceval_gen import ceval_datasets
    from opencompass.configs.datasets.gsm8k.gsm8k_gen import gsm8k_datasets
ceval_subset_map = {
    'computer_network': 'STEM',
    'operating_system': 'STEM',
    'computer_architecture': 'STEM',
}

filtered_ceval = [
    d for d in ceval_datasets 
    if any(key in d.get('abbr', '') for key in ceval_subset_map.keys())
]
for d in gsm8k_datasets:
    d['reader_cfg']['test_range'] = '[0:100]'

datasets = filtered_ceval + gsm8k_datasets