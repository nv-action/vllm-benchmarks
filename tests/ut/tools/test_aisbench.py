import asyncio
import json
import runpy
import sys
import types
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from tools import aisbench
from tools.profiling import capture
from tools.profiling.steady_state import unavailable_steady_state


def test_profiled_request_config_loads_with_mmengine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    config_class = pytest.importorskip("mmengine.config").Config

    class VLLMCustomAPIChat:
        async def stream_infer(self, *_):
            return "stream"

        async def text_infer(self, *_):
            return "text"

    for name in ("ais_bench", "ais_bench.benchmark", "ais_bench.benchmark.models"):
        module = ModuleType(name)
        module.__spec__ = ModuleSpec(name, loader=None)
        monkeypatch.setitem(sys.modules, name, module)
    sys.modules["ais_bench.benchmark.models"].VLLMCustomAPIChat = VLLMCustomAPIChat
    module_name = "tools.profiling.aisbench_request_marker"
    previous_profile_model = sys.modules.pop(module_name, None)
    try:
        request_conf = tmp_path / "vllm_api_stream_chat.py"
        request_conf.write_text(
            "from ais_bench.benchmark.models import VLLMCustomAPIChat\n"
            "models = [\n"
            "    dict(\n"
            "        type=VLLMCustomAPIChat,\n"
            "        model='test',\n"
            "        host_port=8000,\n"
            "    )\n"
            "]\n",
            encoding="utf-8",
        )
        monkeypatch.setattr(aisbench, "REQUEST_CONF_DIR", str(tmp_path))
        runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
        runner.__dict__.update(
            model="test-model",
            model_path="/model",
            port=8001,
            host_ip="localhost",
            max_out_len=1024,
            batch_size=1,
            trust_remote_code=True,
            request_conf="vllm_api_stream_chat",
            request_rate=0,
            top_p=None,
            top_k=None,
            seed=None,
            min_p=None,
            presence_penalty=None,
            repetition_penalty=None,
            thinking=False,
            reasoning_effort=None,
            task_type="performance",
            temperature=None,
            no_pred=False,
            profile_targets=(object(),),
            profile_marker=tmp_path / "first_request",
            dataset_conf="gsm8k/perf",
            num_prompts=1,
        )
        runner._init_request_conf()
        custom = tmp_path / "vllm_api_stream_chat_custom.py"
        assert "profiled_model(" not in custom.read_text(encoding="utf-8")
        popen = MagicMock()
        monkeypatch.setattr(aisbench.subprocess, "Popen", popen)
        runner._run_aisbench_task()
        assert popen.call_args.kwargs["env"]["ASCEND_PROFILE_REQUEST_MARKER"] == str(runner.profile_marker)
        monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(runner.profile_marker))
        model = config_class.fromfile(str(custom)).models[0]["type"]()
        assert asyncio.run(model.stream_infer({}, None)) == "stream"
        first_request = runner.profile_marker.read_text()
        assert asyncio.run(model.text_infer({}, None)) == "text"
        assert runner.profile_marker.read_text() == first_request
    finally:
        sys.modules.pop(module_name, None)
        if previous_profile_model is not None:
            sys.modules[module_name] = previous_profile_model


def test_performance_analysis_runs_before_baseline_assertion(monkeypatch: pytest.MonkeyPatch):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.threshold = 1
    runner.baseline = 100
    runner.input_throughput_threshold = None
    runner.tpot_threshold = None
    calls: list[str] = []

    def get_result() -> None:
        calls.append("result")
        runner.result_json = {"Output Token Throughput": {"total": "1 token/s"}}

    monkeypatch.setattr(runner, "_get_result_performance", get_result)
    monkeypatch.setattr(runner, "_try_analyze_steady_state", lambda: calls.append("steady"))

    with pytest.raises(AssertionError):
        runner._performance_verify()

    assert calls == ["result", "steady"]


def test_performance_analysis_runs_when_result_is_unreadable(monkeypatch: pytest.MonkeyPatch):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    analyze = MagicMock()
    monkeypatch.setattr(runner, "_get_result_performance", MagicMock(side_effect=FileNotFoundError("missing result")))
    monkeypatch.setattr(runner, "_try_analyze_steady_state", analyze)

    with pytest.raises(FileNotFoundError, match="missing result"):
        runner._performance_verify()

    analyze.assert_called_once_with()


@pytest.mark.parametrize("reasoning_effort", [None, "low"])
@pytest.mark.parametrize("profiling", [False, True])
def test_request_config_reasoning_effort(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reasoning_effort: str | None, profiling
):
    request_conf = tmp_path / "vllm_api_general_chat.py"
    request_conf.write_text(
        "model='test',\n"
        "host_port=8000,\n"
        "host_ip='localhost',\n"
        "max_out_len=1024,\n"
        "batch_size=1,\n"
        "trust_remote_code=True,\n"
        "generation_kwargs=dict(\n"
        "    temperature=0,\n"
        "    ignore_eos=False,\n"
        "),\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(aisbench, "REQUEST_CONF_DIR", str(tmp_path))
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        model="test-model",
        port=8001,
        host_ip="localhost",
        max_out_len=65536,
        batch_size=32,
        trust_remote_code=True,
        request_conf="vllm_api_general_chat",
        top_p=None,
        top_k=None,
        seed=None,
        min_p=None,
        presence_penalty=None,
        repetition_penalty=None,
        thinking=True,
        reasoning_effort=reasoning_effort,
        task_type="accuracy",
        temperature=None,
        no_pred=False,
        profile_targets=(object(),) if profiling else (),
    )

    runner._init_request_conf()

    content = (tmp_path / "vllm_api_general_chat_custom.py").read_text(encoding="utf-8")
    assert 'chat_template_kwargs={"thinking": True}' in content
    assert runner.profile_targets == ()
    assert "ProfiledVLLMCustomAPIChat" not in content
    if reasoning_effort is None:
        assert "reasoning_effort=" not in content
    else:
        assert 'reasoning_effort="low"' in content


@pytest.mark.parametrize("threshold", [1])
def test_steady_state_failure_does_not_affect_performance_verification(monkeypatch, caplog, threshold):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        result_json={"Output Token Throughput": {"total": "10token/s"}} if threshold is not None else {},
        result_csv=None,
        baseline=10,
        threshold=threshold,
        input_throughput_threshold=None,
        tpot_threshold=None,
    )
    monkeypatch.setattr(runner, "_get_result_performance", lambda: None)

    def fail_analysis() -> None:
        raise RuntimeError("steady-state failure: private-service:8000")

    monkeypatch.setattr(runner, "_try_analyze_steady_state", fail_analysis)

    runner._performance_verify()
    assert "RuntimeError" in caplog.text
    assert "private-service" not in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


@pytest.mark.parametrize("method", ["stream_infer", "text_infer"])
@pytest.mark.parametrize("marker_fails", [False, True])
def test_request_marker_never_suppresses_inference(tmp_path, monkeypatch, method, marker_fails):
    class Model:
        async def stream_infer(self, body, output):
            if body == "fail":
                raise RuntimeError("inference failure")
            return body

        text_infer = stream_infer

    for name in ("ais_bench", "ais_bench.benchmark", "ais_bench.benchmark.models"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["ais_bench.benchmark.models"].VLLMCustomAPIChat = Model
    calls = []

    def mark(path):
        calls.append(path)
        if marker_fails:
            raise PermissionError("marker unavailable")

    monkeypatch.setattr(capture, "record_first_request", mark)
    monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(tmp_path / "marker"))
    namespace = runpy.run_path(str(Path(capture.__file__).with_name("aisbench_request_marker.py")))
    model = namespace["ProfiledVLLMCustomAPIChat"]()
    infer = getattr(model, method)
    assert asyncio.run(infer("first", None)) == "first"
    assert asyncio.run(infer("second", None)) == "second"
    with pytest.raises(RuntimeError, match="inference failure"):
        asyncio.run(infer("fail", None))
    assert len(calls) == 1


@pytest.mark.parametrize("failure", [None, "save", "serialize", "render"])
def test_steady_state_outputs_are_independent_and_namespaced(tmp_path, monkeypatch, capsys, failure):
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path)
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        case_name="performance", profile_case_name="config-a__performance", performance_result_dir=None, request_rate=0
    )
    result = unavailable_steady_state(target_concurrency=1, reason="no timing data")
    if failure == "save":
        (tmp_path / runner.profile_case_name).write_text("a file blocks directory creation")
    if failure == "serialize":

        def fail_serialize(*_, **kwargs):
            raise TypeError("serialization failed")

        monkeypatch.setattr(aisbench.json, "dump", fail_serialize)
    if failure == "render":

        def fail_render(*_, **kwargs):
            raise RuntimeError("render failed")

        monkeypatch.setattr(aisbench, "render_terminal", fail_render)
        with pytest.raises(RuntimeError, match="render failed"):
            runner._emit_steady_state(result, None)
    else:
        runner._emit_steady_state(result, None)
        assert "[STEADY STATE] performance" in capsys.readouterr().out
    if failure not in {"save", "serialize"}:
        assert json.loads((tmp_path / runner.profile_case_name / "summary.json").read_text())["status"] == "unavailable"
    if failure is None:
        runner.profile_case_name = "config-b__performance"
        runner._emit_steady_state(result, None)
        assert len(list(tmp_path.glob("*/summary.json"))) == 2


def test_profile_finish_failure_is_optional(caplog):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)

    class Controller:
        def finish(self):
            raise OSError("simulated")

    runner._finish_profiling(Controller())
    assert "Profiling collection failed (OSError)" in caplog.text
