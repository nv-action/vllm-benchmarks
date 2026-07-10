# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.

from vllm import SamplingParams

from tests.e2e.pull_request.extract_hidden_states_utils import (
    HYBRID_AUX_HIDDEN_STATE_LAYER_IDS,
    HYBRID_MODEL,
    ExtractHiddenStatesCase,
    run_extract_hidden_states_case,
)


def test_extract_hidden_states_hybrid_dummy_eager() -> None:
    case = ExtractHiddenStatesCase(
        model_name=HYBRID_MODEL,
        aux_hidden_state_layer_ids=HYBRID_AUX_HIDDEN_STATE_LAYER_IDS,
        prompts=[
            "Hello world",
            "Test prompt with several tokens",
        ],
        enforce_eager=True,
        gpu_memory_utilization=0.4,
        max_model_len=256,
        load_format="dummy",
        verify_nonzero=False,
        verify_token_ids=True,
    )
    run_extract_hidden_states_case(case, SamplingParams(temperature=0, max_tokens=1))
