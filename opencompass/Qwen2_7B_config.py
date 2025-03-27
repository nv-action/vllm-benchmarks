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
        abbr='Qwen2-7B-vLLM-API',
        type=OpenAISDK,
        key='EMPTY',
        openai_api_base='http://127.0.0.1:8000/v1',
        path='Qwen/Qwen2-7B', 
        tokenizer_path='Qwen/Qwen2-7B', 
        rpm_verbose=True, 
        meta_template=api_meta_template, 
        query_per_second=1, 
        max_out_len=1024,
        max_seq_len=4096,
        temperature=0.01,
        batch_size=8, 
        retry=3, 
    )
]
with read_base():
    from opencompass.configs.datasets.ceval.ceval_gen import ceval_datasets
    from opencompass.configs.datasets.gsm8k.gsm8k_gen import gsm8k_datasets

datasets = ceval_datasets + gsm8k_datasets