# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
# Licensed under the Apache License, Version 2.0

import pytest
from vllm import SamplingParams

from tests.e2e.pull_request.extract_hidden_states_utils import (
    DENSE_AUX_HIDDEN_STATE_LAYER_IDS,
    DENSE_MODEL,
    ExtractHiddenStatesCase,
    run_extract_hidden_states_case,
)

DENSE_CASES = [
    pytest.param(
        ExtractHiddenStatesCase(
            model_name=DENSE_MODEL,
            aux_hidden_state_layer_ids=DENSE_AUX_HIDDEN_STATE_LAYER_IDS,
            prompts=[
                "Hello, how are you?",
                "What is machine learning?",
                "Explain quantum computing briefly.",
            ],
            enforce_eager=True,
            gpu_memory_utilization=0.8,
            max_num_seqs=16,
        ),
        id="dense_eager",
    ),
    pytest.param(
        ExtractHiddenStatesCase(
            model_name=DENSE_MODEL,
            aux_hidden_state_layer_ids=DENSE_AUX_HIDDEN_STATE_LAYER_IDS,
            prompts=[
                "Hello, how are you?",
                "What is machine learning?",
            ],
            enforce_eager=False,
            max_num_seqs=16,
        ),
        id="dense_aclgraph",
    ),
]


@pytest.mark.parametrize("case", DENSE_CASES)
def test_extract_hidden_states(case: ExtractHiddenStatesCase) -> None:
    run_extract_hidden_states_case(case, SamplingParams(temperature=0, max_tokens=1))
