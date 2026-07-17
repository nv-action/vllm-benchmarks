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
#

from __future__ import annotations

from vllm import SamplingParams

from tests.e2e.conftest import VllmRunner


def calculate_acceptance_per_pos(
    metrics: list,
    num_speculative_tokens: int,
    counter_type: type,
    vector_type: type,
) -> list[float]:
    num_drafts = 0
    accepted_per_pos = [0] * num_speculative_tokens
    for metric in metrics:
        if metric.name == "vllm:spec_decode_num_drafts":
            assert isinstance(metric, counter_type)
            num_drafts += metric.value  # type: ignore[attr-defined]
        elif metric.name == "vllm:spec_decode_num_accepted_tokens_per_pos":
            assert isinstance(metric, vector_type)
            for pos in range(len(metric.values)):  # type: ignore[attr-defined]
                accepted_per_pos[pos] += metric.values[pos]  # type: ignore[attr-defined]
    return [a / num_drafts for a in accepted_per_pos]


def run_dense_graph_mode(
    model: str,
    max_tokens: int,
    enforce_eager: bool,
    compilation_config: dict,
) -> None:
    prompts = [
        "Hello, my name is",
        "The president of the United States is",
        "The capital of France is",
        "The future of AI is",
    ]

    sampling_params = SamplingParams(max_tokens=max_tokens, temperature=0.0)
    with VllmRunner(
        model,
        max_model_len=1024,
        enforce_eager=enforce_eager,
        compilation_config=compilation_config,
    ) as runner:
        outputs = runner.model.generate(prompts, sampling_params)

    if model != "Qwen/Qwen3-0.6B":
        return

    expected_outputs = [
        " Lina. I'm a 22-year-old student from China.",
        " the same as the president of the United Nations. This is because the president",
        " Paris. The capital of France is also the capital of the Republic of France",
        " not just about the technology itself but also about the human aspect-how we",
    ]

    misses = 0
    for output, expected_output in zip(outputs, expected_outputs):
        if output.outputs[0].text[:10] != expected_output[:10]:
            misses += 1
            print(f"output: {output.outputs[0].text}")
            print(f"expected_output: {expected_output}")

    assert misses == 0
