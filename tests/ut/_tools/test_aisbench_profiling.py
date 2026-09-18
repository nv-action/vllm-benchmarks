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


def test_profile_batch_uses_one_full_batch_without_warmup(monkeypatch: pytest.MonkeyPatch) -> None:
    aisbench = _import_aisbench(monkeypatch)
    captured: list[dict] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.append(kwargs)

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
    profile_context = object()

    aisbench.run_aisbench_profile_batch(
        model="model",
        port=8000,
        aisbench_cases=[original_case],
        profile_context=profile_context,
    )

    assert captured[0]["aisbench_config"] == {
        "case_type": "performance",
        "num_prompts": 1,
        "num_warmups": 0,
        "batch_size": 1,
        "max_out_len": 1500,
    }
    assert "task_context" not in captured[0]
    assert captured[1]["aisbench_config"] == {
        "case_type": "performance",
        "num_prompts": 32,
        "num_warmups": 0,
        "batch_size": 32,
        "max_out_len": 1500,
    }
    assert captured[1]["task_context"] is profile_context
    assert all(call["verify"] is False for call in captured)
    assert original_case["num_prompts"] == 128
    assert original_case["batch_size"] == 32


def test_profile_batch_prefers_performance_case_and_caps_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    aisbench = _import_aisbench(monkeypatch)
    captured: list[dict] = []

    class FakeRunner:
        def __init__(self, **kwargs):
            captured.append(kwargs)

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return None

    monkeypatch.setattr(aisbench, "AisbenchRunner", FakeRunner)
    cases = [
        {
            "case_type": "accuracy",
            "batch_size": 32,
            "max_out_len": 65536,
        },
        {
            "case_type": "performance",
            "num_prompts": 140,
            "batch_size": 35,
            "max_out_len": 1500,
        },
    ]

    aisbench.run_aisbench_profile_batch(
        model="model",
        port=8000,
        aisbench_cases=cases,
        profile_context=object(),
    )

    assert captured[0]["aisbench_config"] == {
        "case_type": "performance",
        "num_prompts": 1,
        "num_warmups": 0,
        "batch_size": 1,
        "max_out_len": 1500,
    }
    assert captured[1]["aisbench_config"] == {
        "case_type": "performance",
        "num_prompts": aisbench.PROFILE_BATCH_REQUESTS,
        "num_warmups": 0,
        "batch_size": aisbench.PROFILE_BATCH_REQUESTS,
        "max_out_len": 1500,
    }
    assert cases[1]["num_prompts"] == 140
    assert cases[1]["batch_size"] == 35


def test_aisbench_enters_task_context_after_initialization(monkeypatch: pytest.MonkeyPatch) -> None:
    aisbench = _import_aisbench(monkeypatch)
    events: list[str] = []

    class RecordingContext:
        def __enter__(self):
            events.append("start_profile")

        def __exit__(self, exc_type, exc_value, traceback):
            events.append("stop_profile")

    monkeypatch.setattr(aisbench.AisbenchRunner, "_init_dataset_conf", lambda self: events.append("init_dataset"))
    monkeypatch.setattr(aisbench.AisbenchRunner, "_init_request_conf", lambda self: events.append("init_request"))
    monkeypatch.setattr(aisbench.AisbenchRunner, "_run_aisbench_task", lambda self: events.append("run_task"))
    monkeypatch.setattr(aisbench.AisbenchRunner, "_wait_for_task", lambda self: events.append("wait_task"))

    aisbench.AisbenchRunner(
        model="model",
        port=8000,
        aisbench_config={
            "case_type": "performance",
            "dataset_path_local": "/dataset",
            "model_path": "/model",
            "request_conf": "vllm_api_stream_chat",
            "dataset_conf": "gsm8k/config",
            "num_prompts": 32,
            "num_warmups": 0,
            "max_out_len": 1500,
            "batch_size": 32,
        },
        verify=False,
        task_context=RecordingContext(),
    )

    assert events == [
        "init_dataset",
        "init_request",
        "start_profile",
        "run_task",
        "wait_task",
        "stop_profile",
    ]


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
    runner.num_prompts = 32
    runner.num_warmups = 0

    runner._run_aisbench_task()

    assert "--num-prompts 32" in commands[0]
    assert "--num-warmups 0" in commands[0]
