#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
# Adapted from vllm/tests/basic_correctness/test_basic_correctness.py
#
"""Compare the short outputs of HF and vLLM when using greedy sampling.

Run `pytest tests/test_offline_inference.py`.
"""
import os

import pytest
import vllm  # noqa: F401

import vllm_ascend  # noqa: F401
from tests.e2e.conftest import VllmRunner

MODELS = [
    "Qwen/Qwen3-0.6B-Base",
    "Qwen/Qwen2.5-7B-Instruct"
]

os.environ["PYTORCH_NPU_ALLOC_CONF"] = "max_split_size_mb:256"
os.environ["VLLM_WORKER_MULTIPROC_METHOD"] = "spawn"

@pytest.mark.parametrize("model", MODELS)
@pytest.mark.parametrize("dtype", ["float16"])
@pytest.mark.parametrize("max_tokens", [5])
def test_models(model: str, dtype: str, max_tokens: int) -> None:
    example_prompts = [
        "Hello, my name is",
        "The future of AI is",
    ]
    
    llm_config = {
        "tensor_parallel_size": 1,
        "enforce_eager": True,
        "dtype": dtype,
        "gpu_memory_utilization": 0.95,
        "max_model_len": 2048,
        "compilation_config": {
            "custom_ops": ["none", "+rms_norm", "+rotary_embedding"],
        },
    }

    with VllmRunner(model, **llm_config) as vllm_model:
        vllm_model.generate_greedy(example_prompts, max_tokens)