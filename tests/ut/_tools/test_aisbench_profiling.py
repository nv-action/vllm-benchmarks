import importlib
import sys
from types import ModuleType

import pytest


def _import_aisbench(monkeypatch: pytest.MonkeyPatch):
    huggingface_hub = ModuleType("huggingface_hub")
    pandas = ModuleType("pandas")
    modelscope = ModuleType("modelscope")
    modelscope.snapshot_download = lambda *args, **kwargs: None  # type: ignore[attr-defined]

    monkeypatch.setitem(sys.modules, "huggingface_hub", huggingface_hub)
    monkeypatch.setitem(sys.modules, "pandas", pandas)
    monkeypatch.setitem(sys.modules, "modelscope", modelscope)
    sys.modules.pop("tools.aisbench", None)
    return importlib.import_module("tools.aisbench")


def test_profile_request_uses_one_prompt_without_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    aisbench = _import_aisbench(monkeypatch)
    captured: dict = {}

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(aisbench, "AisbenchRunner", FakeRunner)
    original_case = {
        "case_type": "performance",
        "num_prompts": 128,
        "batch_size": 32,
        "max_out_len": 1500,
    }

    aisbench.run_aisbench_profile_request(
        model="model",
        port=8000,
        aisbench_cases=[original_case],
    )

    assert captured["aisbench_config"] == {
        "case_type": "performance",
        "num_prompts": 1,
        "num_warmups": 0,
        "batch_size": 1,
        "max_out_len": 1500,
    }
    assert captured["verify"] is False
    assert original_case["num_prompts"] == 128
    assert original_case["batch_size"] == 32


def test_aisbench_command_forwards_zero_warmups(monkeypatch: pytest.MonkeyPatch) -> None:
    aisbench = _import_aisbench(monkeypatch)
    commands: list[str] = []

    def popen(command: str, *, shell: bool):
        commands.append(command)
        return object()

    monkeypatch.setattr(aisbench.subprocess, "Popen", popen)
    runner = object.__new__(aisbench.AisbenchRunner)
    runner.dataset_conf = "gsm8k/config"
    runner.task_type = "performance"
    runner.request_conf = "vllm_api_stream_chat"
    runner.num_prompts = 1
    runner.num_warmups = 0

    runner._run_aisbench_task()

    assert "--num-prompts 1" in commands[0]
    assert "--num-warmups 0" in commands[0]
