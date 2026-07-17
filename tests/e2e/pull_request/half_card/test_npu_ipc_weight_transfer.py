#
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
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
#
"""End-to-end test for the NPU IPC weight transfer engine.

Unlike the HCCL engine, NPU IPC requires the trainer and the inference worker
to be co-located on the *same* physical NPU chip, so only a single NPU is
needed. The trainer model is built from the architecture config with random
weights (download-free); set ``WEIGHT_TRANSFER_TEST_MODEL=/path/to/checkpoint``
to share real weights instead. See ``examples/rl/rlhf_http_npu_ipc.py`` for the
end-user workflow.
"""

import contextlib
import faulthandler
import gc
import multiprocessing
import os
import traceback
from multiprocessing.connection import Connection

import pytest
import requests
import torch
import torch_npu  # noqa: F401  # registers the NPU backend
from transformers import AutoConfig, AutoModelForCausalLM

from tests.e2e.conftest import RemoteOpenAIServer

MODEL_NAME = "Qwen/Qwen3-0.6B"

INFERENCE_DEVICE_INDEX = 0

PROMPTS = [
    "Hello, my name is",
    "The capital of France is",
]

UPDATE_TIMEOUT = 300
CONTROL_TIMEOUT = 60
TRAINER_TRANSFER_TIMEOUT = 600
TRAINER_EXIT_TIMEOUT = 60
TRAINER_TERMINATE_TIMEOUT = 30
TRAINER_STACK_DUMP_INTERVAL = 30


def _build_trainer_model(device_index: int):
    device = f"npu:{device_index}"
    override_path = os.getenv("WEIGHT_TRANSFER_TEST_MODEL")
    if override_path:
        model = AutoModelForCausalLM.from_pretrained(override_path, dtype=torch.bfloat16)
    else:
        config = AutoConfig.from_pretrained(MODEL_NAME, trust_remote_code=True)
        model = AutoModelForCausalLM.from_config(config)
    model = model.to(device=device, dtype=torch.bfloat16)
    model.eval()
    return model


def _post(server: RemoteOpenAIServer, route: str, *, json=None, timeout=CONTROL_TIMEOUT):
    response = requests.post(server.url_for(route), json=json, timeout=timeout)
    response.raise_for_status()
    return response


def _generate(client, model, prompts):
    completions = []
    for prompt in prompts:
        response = client.completions.create(model=model, prompt=prompt, max_tokens=16, temperature=0)
        completions.append(response.choices[0].text)
    return completions


def _has_lifecycle_endpoints(server: RemoteOpenAIServer) -> bool:
    """Probe ``/start_weight_update``; also performs the actual call when present."""
    try:
        response = requests.post(
            server.url_for("start_weight_update"),
            json={"is_checkpoint_format": True},
            timeout=CONTROL_TIMEOUT,
        )
    except requests.RequestException:
        return False
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def _trainer_log(message: str) -> None:
    print(f"[NPU IPC trainer] {message}", flush=True)


def _trainer_send_weights_process(server_url: str, result_pipe: Connection) -> None:
    """Build and send trainer weights in an isolated NPU process."""
    train_model = None
    faulthandler.enable()
    faulthandler.dump_traceback_later(TRAINER_STACK_DUMP_INTERVAL, repeat=True)
    try:
        _trainer_log(f"selecting npu:{INFERENCE_DEVICE_INDEX}")
        torch.npu.set_device(INFERENCE_DEVICE_INDEX)
        _trainer_log("building trainer model")
        train_model = _build_trainer_model(INFERENCE_DEVICE_INDEX)
        _trainer_log("trainer model ready")
        os.environ["VLLM_ALLOW_INSECURE_SERIALIZATION"] = "1"

        from vllm_ascend.distributed.weight_transfer.npu_ipc_engine import (
            NPUIPCTrainerSendWeightsArgs,
            NPUIPCWeightTransferEngine,
        )

        trainer_args = NPUIPCTrainerSendWeightsArgs(send_mode="http", url=server_url)
        _trainer_log("sending IPC handles to /update_weights")
        NPUIPCWeightTransferEngine.trainer_send_weights(
            iterator=train_model.named_parameters(),
            trainer_args=trainer_args,
        )
        _trainer_log("/update_weights completed")
        result_pipe.send(("transfer_complete", ""))
    except BaseException:  # noqa: BLE001 - propagate the child traceback to pytest
        error = traceback.format_exc()
        print(error, flush=True)
        with contextlib.suppress(BrokenPipeError, EOFError, OSError):
            result_pipe.send(("transfer_error", error))
    finally:
        try:
            _trainer_log("releasing trainer NPU resources")
            del train_model
            gc.collect()
            torch.npu.synchronize()
            torch.npu.empty_cache()
            _trainer_log("trainer NPU resources released")
        except BaseException:  # noqa: BLE001 - cleanup failures must be visible to the parent
            error = traceback.format_exc()
            print(error, flush=True)
            with contextlib.suppress(BrokenPipeError, EOFError, OSError):
                result_pipe.send(("cleanup_error", error))
        finally:
            faulthandler.cancel_dump_traceback_later()
            result_pipe.close()


def _terminate_trainer_process(process: multiprocessing.Process) -> None:
    if not process.is_alive():
        return
    process.terminate()
    process.join(TRAINER_TERMINATE_TIMEOUT)
    if process.is_alive():
        process.kill()
        process.join(TRAINER_TERMINATE_TIMEOUT)


def _run_trainer_process(server_url: str) -> None:
    context = multiprocessing.get_context("spawn")
    result_pipe, child_pipe = context.Pipe(duplex=False)
    process = context.Process(
        target=_trainer_send_weights_process,
        args=(server_url, child_pipe),
        name="npu-ipc-trainer",
    )
    process.start()
    child_pipe.close()

    messages = []
    try:
        if not result_pipe.poll(TRAINER_TRANSFER_TIMEOUT):
            _terminate_trainer_process(process)
            pytest.fail(
                f"trainer did not finish IPC transfer within {TRAINER_TRANSFER_TIMEOUT}s",
                pytrace=False,
            )

        with contextlib.suppress(EOFError):
            messages.append(result_pipe.recv())

        process.join(TRAINER_EXIT_TIMEOUT)
        if process.is_alive():
            _terminate_trainer_process(process)
            first_error = next((payload for status, payload in messages if status.endswith("_error")), "")
            pytest.fail(
                f"{first_error}\ntrainer process did not exit within {TRAINER_EXIT_TIMEOUT}s after transfer",
                pytrace=False,
            )

        while result_pipe.poll():
            try:
                messages.append(result_pipe.recv())
            except EOFError:
                break
    finally:
        result_pipe.close()
        _terminate_trainer_process(process)

    errors = [payload for status, payload in messages if status.endswith("_error")]
    if errors:
        pytest.fail("\n".join(errors), pytrace=False)
    if not any(status == "transfer_complete" for status, _ in messages):
        pytest.fail(
            f"trainer process exited with code {process.exitcode} before reporting transfer completion",
            pytrace=False,
        )
    if process.exitcode != 0:
        pytest.fail(f"trainer process exited with code {process.exitcode}", pytrace=False)


@pytest.mark.skipif(
    torch.npu.device_count() < 1,
    reason="NPU IPC weight transfer e2e test requires at least 1 NPU.",
)
def test_npu_ipc_weight_transfer_updates_server_weights():
    from vllm.utils.network_utils import get_open_port

    port = get_open_port()
    server_args = [
        "--enforce-eager",
        "--load-format",
        "dummy",
        "--weight-transfer-config",
        '{"backend": "ipc"}',
        "--max-model-len",
        "1024",
        # IPC co-locates the trainer on the same NPU, so leave room for it.
        "--gpu-memory-utilization",
        "0.5",
        "--port",
        str(port),
        "--trust-remote-code",
    ]
    # VLLM_SERVER_DEV_MODE registers the dev endpoints; insecure serialization
    # lets the server unpickle the IPC handles sent over HTTP. Pin the server to
    # physical NPU 0 so its IPC UUID matches the trainer below.
    env_dict = {
        "VLLM_SERVER_DEV_MODE": "1",
        "VLLM_ALLOW_INSECURE_SERIALIZATION": "1",
        "ASCEND_RT_VISIBLE_DEVICES": str(INFERENCE_DEVICE_INDEX),
        "VLLM_ASCEND_ENABLE_NZ": "0",
    }

    with RemoteOpenAIServer(
        MODEL_NAME,
        vllm_serve_args=server_args,
        server_host="127.0.0.1",
        server_port=port,
        env_dict=env_dict,
        auto_port=False,
    ) as server:
        client = server.get_client()

        outputs_before = _generate(client, MODEL_NAME, PROMPTS)

        _post(server, "init_weight_transfer_engine", json={"init_info": {}})

        _post(server, "pause")
        # The probe performs /start_weight_update when present, so it must not
        # be called again below. Older vLLM without the lifecycle endpoints is
        # out of scope for this IPC test.
        if not _has_lifecycle_endpoints(server):
            _post(server, "resume")
            pytest.skip("vLLM build lacks the /start_weight_update lifecycle endpoints required by NPU IPC.")

        # Keep trainer NPU state out of the pytest process. The child shares
        # physical NPU 0 with the server and reports failures before cleanup,
        # so a stuck torch-npu/resource-tracker shutdown can be force-killed.
        _run_trainer_process(server.url_root)

        _post(server, "finish_weight_update")
        _post(server, "resume")

        outputs_after = _generate(client, MODEL_NAME, PROMPTS)

    assert outputs_after != outputs_before, "server weights did not change after NPU IPC transfer"
