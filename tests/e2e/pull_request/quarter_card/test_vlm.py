# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
# Copyright 2023 The vLLM team.
# Licensed under the Apache License, Version 2.0

import os
from unittest.mock import patch

import pytest
from vllm import SamplingParams
from vllm.assets.audio import AudioAsset

from tests.e2e.conftest import VllmRunner
from tests.e2e.pull_request.vlm_utils import run_multimodal_vl


@pytest.mark.parametrize("vl_config", ["hunyuan-vl"], indirect=True)
@patch.dict(os.environ, {"VLLM_WORKER_MULTIPROC_METHOD": "spawn"})
def test_multimodal_vl(vl_config) -> None:
    run_multimodal_vl(vl_config)


@patch.dict(os.environ, {"VLLM_WORKER_MULTIPROC_METHOD": "spawn"})
def test_whisper() -> None:
    model = "openai-mirror/whisper-large-v3-turbo"
    prompts = ["<|startoftranscript|><|en|><|transcribe|><|notimestamps|>"]
    audios = [AudioAsset("mary_had_lamb").audio_and_sample_rate]
    sampling_params = SamplingParams(temperature=0.2, max_tokens=10, stop_token_ids=None)

    with VllmRunner(
        model, max_model_len=448, max_num_seqs=5, dtype="bfloat16", block_size=128, gpu_memory_utilization=0.9
    ) as runner:
        outputs = runner.generate(prompts=prompts, audios=audios, sampling_params=sampling_params)

    assert outputs is not None, "Generated outputs should not be None."
    assert len(outputs) > 0, "Generated outputs should not be empty."
