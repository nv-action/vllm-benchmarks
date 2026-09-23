import asyncio
import json
import sys
from importlib.machinery import ModuleSpec
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest

from tools import aisbench
from tools.aisbench_perf_data import TimingLoadResult
from tools.benchmark_steady_state import RequestTiming, TimingLoadStats


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
    previous_profile_model = sys.modules.pop("tools.aisbench_profile_model", None)
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
        assert popen.call_args.kwargs["env"]["NIGHTLY_PROFILE_REQUEST_MARKER"] == str(runner.profile_marker)
        monkeypatch.setenv("NIGHTLY_PROFILE_REQUEST_MARKER", str(runner.profile_marker))
        model = config_class.fromfile(str(custom)).models[0]["type"]()
        assert asyncio.run(model.stream_infer({}, None)) == "stream"
        first_request = runner.profile_marker.read_text()
        assert asyncio.run(model.text_infer({}, None)) == "text"
        assert runner.profile_marker.read_text() == first_request
    finally:
        sys.modules.pop("tools.aisbench_profile_model", None)
        if previous_profile_model is not None:
            sys.modules["tools.aisbench_profile_model"] = previous_profile_model


@pytest.mark.parametrize("reasoning_effort", [None, "low"])
def test_request_config_reasoning_effort(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reasoning_effort: str | None):
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
    )

    runner._init_request_conf()

    content = (tmp_path / "vllm_api_general_chat_custom.py").read_text(encoding="utf-8")
    assert 'chat_template_kwargs={"thinking": True}' in content
    if reasoning_effort is None:
        assert "reasoning_effort=" not in content
    else:
        assert 'reasoning_effort="low"' in content


def test_try_analyze_steady_state_writes_one_flushed_group_without_changing_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    original_result = [object(), {"Output Token Throughput": {"total": "1 token/s"}}]
    runner.__dict__.update(
        case_name="perf/example",
        batch_size=2,
        request_rate=0,
        performance_result_dir=tmp_path,
        performance_dataset_type="dataset",
        steady_state_result=None,
        result=original_result,
    )
    timings = [
        RequestTiming("a", 0, 12, True),
        RequestTiming("b", 1, 11, True),
    ]
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path / "steady_state")
    monkeypatch.setattr(
        aisbench.AisbenchTimingAdapter,
        "load_request_timings",
        lambda self: TimingLoadResult(timings, TimingLoadStats(2, 2, 2, 0)),
    )
    print_output = MagicMock()
    monkeypatch.setattr("builtins.print", print_output)

    runner._try_analyze_steady_state()

    print_output.assert_called_once()
    output = print_output.call_args.args[0]
    assert print_output.call_args.kwargs == {"flush": True}
    assert output.startswith("::group::🟢 [STEADY STATE] perf/example | FOUND |")
    assert output.endswith("::endgroup::")
    assert "Starting Steady State Analysis" not in output
    assert f"Directory:             {tmp_path}" in output
    assert "Records read:          2" in output
    assert "Request rate:          0" in output
    assert runner.result is original_result
    assert runner.steady_state_result.status == "found"
    summary_path = tmp_path / "steady_state" / "perf_example" / "summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert summary["steady_state"]["start"] == {"time_s": 1, "completed_requests": 0}
    assert summary["steady_state"]["end"] == {"time_s": 11, "completed_requests": 1}


def test_try_analyze_steady_state_is_non_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        case_name="perf",
        batch_size=2,
        request_rate=0,
        performance_result_dir=tmp_path,
        performance_dataset_type="dataset",
        steady_state_result=None,
    )
    monkeypatch.setattr(
        aisbench.AisbenchTimingAdapter,
        "load_request_timings",
        MagicMock(side_effect=RuntimeError("broken timing artifact")),
    )

    runner._try_analyze_steady_state()

    assert runner.steady_state_result is None


def test_rate_controlled_workload_is_skipped_without_reading_timings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        case_name="fixed-qps",
        batch_size=2,
        request_rate=10,
        performance_result_dir=tmp_path,
        performance_dataset_type="dataset",
        steady_state_result=None,
    )
    load_timings = MagicMock(side_effect=AssertionError("adapter must not run"))
    monkeypatch.setattr(aisbench.AisbenchTimingAdapter, "load_request_timings", load_timings)
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path / "steady_state")

    runner._try_analyze_steady_state()

    assert not load_timings.called
    assert runner.steady_state_result.status == "skipped"
    assert "::group::⚪ [STEADY STATE] fixed-qps | SKIPPED | rate-controlled workload" in capsys.readouterr().out
    summary_path = tmp_path / "steady_state" / "fixed-qps" / "summary.json"
    assert json.loads(summary_path.read_text(encoding="utf-8"))["reason"] == "rate-controlled workload"


def test_missing_details_writes_unavailable_summary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.__dict__.update(
        case_name="perf",
        batch_size=2,
        request_rate=0,
        performance_result_dir=tmp_path,
        performance_dataset_type="dataset",
        steady_state_result=None,
    )
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path / "steady_state")

    runner._try_analyze_steady_state()

    assert runner.steady_state_result.status == "unavailable"
    output = capsys.readouterr().out
    assert "::group::🔴 [STEADY STATE] perf | UNAVAILABLE | timing data unavailable" in output
    assert "Reason:" in output
    summary_path = tmp_path / "steady_state" / "perf" / "summary.json"
    assert json.loads(summary_path.read_text(encoding="utf-8"))["status"] == "unavailable"


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
