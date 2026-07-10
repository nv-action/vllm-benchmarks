# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
# This file is a part of the vllm-ascend project.

import os
from unittest.mock import patch

import pytest

from tests.e2e.pull_request.model_runner_v2_utils import run_dense_graph_mode
from vllm_ascend.utils import vllm_version_is

pytestmark = pytest.mark.skipif(
    vllm_version_is("0.23.0"),
    reason="v2 model runner patches not supported on v0.23.0",
)


@pytest.mark.parametrize("model", ["Qwen/Qwen3-0.6B"])
@pytest.mark.parametrize("max_tokens", [32])
@pytest.mark.parametrize("enforce_eager", [False])
@pytest.mark.parametrize(
    "compilation_config",
    [
        pytest.param({"cudagraph_mode": "FULL_DECODE_ONLY"}, id="full_decode_only"),
        pytest.param({}, id="default_full_and_piecewise"),
    ],
)
@patch.dict(os.environ, {"VLLM_USE_V2_MODEL_RUNNER": "1"})
def test_qwen3_dense_graph_mode(
    model: str,
    max_tokens: int,
    enforce_eager: bool,
    compilation_config: dict,
) -> None:
    run_dense_graph_mode(model, max_tokens, enforce_eager, compilation_config)
