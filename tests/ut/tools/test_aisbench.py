import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import Mock

import pytest

from tools import aisbench
from tools.profiling import capture
from tools.profiling.steady_state import unavailable_steady_state


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
        profile_targets=(),
    )

    runner._init_request_conf()

    content = (tmp_path / "vllm_api_general_chat_custom.py").read_text(encoding="utf-8")
    assert 'chat_template_kwargs={"thinking": True}' in content
    if reasoning_effort is None:
        assert "reasoning_effort=" not in content
    else:
        assert 'reasoning_effort="low"' in content


@pytest.fixture
def benchmark_setup(tmp_path, monkeypatch):
    """Provide local AISBench inputs and replace only external execution."""
    for name, value in capture.ProfileSpec(enabled=True).environment().items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("ASCEND_PROFILE_ROOT", str(tmp_path))
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path / "steady_state")
    monkeypatch.setattr(aisbench, "REQUEST_CONF_DIR", str(tmp_path))
    monkeypatch.setattr(aisbench, "DATASET_CONF_DIR", str(tmp_path))
    instance = capture.ServeInstance.from_endpoint("server", "http://server:8000", root=tmp_path)
    capture.ServeManifest((instance,)).write(tmp_path / "serve_manifest.json")
    (tmp_path / "chat.py").write_text("models = [dict(\n    type=VLLMCustomAPIChat,\n    model='model',\n)]\n")
    (tmp_path / "dataset.py").write_text("inferencer = dict(type=GenInferencer)\npath='dataset'\n")
    config = {
        "case_name": "load",
        "profile_case_name": "model__load",
        "case_type": "performance",
        "dataset_path_local": str(tmp_path),
        "model_path": str(tmp_path),
        "request_conf": "chat",
        "dataset_conf": "dataset",
        "max_out_len": 16,
        "batch_size": 2,
    }
    monkeypatch.setattr(aisbench.AisbenchRunner, "_run_aisbench_task", lambda self: None)
    monkeypatch.setattr(aisbench.AisbenchRunner, "_wait_for_task", lambda self: None)
    controller = Mock()
    controller.finish.return_value = {"status": "success"}
    monkeypatch.setattr(aisbench, "ProfileController", Mock(return_value=controller))
    collect = Mock()
    monkeypatch.setattr(aisbench, "collect_raw_artifacts", collect)
    return config, controller, collect


def test_benchmark_profile_lifecycle(tmp_path, benchmark_setup):
    config, controller, collect = benchmark_setup
    marker = tmp_path / "steady_state" / "model__load" / "first_request"
    marker.parent.mkdir(parents=True)
    marker.write_text("previous timestamp")
    runner = aisbench.AisbenchRunner("model", 8000, config, verify=False)
    generated = (tmp_path / "dataset_custom.py").read_text()
    compile(generated, "dataset_custom.py", "exec")
    assert "type=TimedGenInferencer" in generated
    assert "type=VLLMCustomAPIChat" in (tmp_path / "chat_custom.py").read_text()
    assert not marker.exists()
    controller.start.assert_called_once()
    controller.finish.assert_called_once()
    collect.assert_called_once_with(tmp_path, "model__load", runner.profile_targets, {"status": "success"})


@pytest.mark.parametrize("error_type", [ValueError, KeyboardInterrupt])
def test_profile_cleanup_does_not_mask_benchmark_error(benchmark_setup, monkeypatch, error_type):
    config, controller, collect = benchmark_setup
    benchmark_error = error_type("benchmark failed")
    monkeypatch.setattr(aisbench.AisbenchRunner, "_wait_for_task", Mock(side_effect=benchmark_error))
    controller.finish.side_effect = OSError("profile shutdown failed")
    with pytest.raises(error_type) as error:
        aisbench.AisbenchRunner("model", 8000, config, verify=False)
    assert error.value is benchmark_error
    controller.finish.assert_called_once()
    collect.assert_not_called()


@pytest.mark.parametrize("selected", [False, True])
def test_aisbench_process_receives_marker_and_case_capture_mode(tmp_path, monkeypatch, selected):
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "true")
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.dataset_conf = "dataset"
    runner.request_conf = "chat"
    runner.task_type = "performance"
    runner.num_prompts = None
    runner.profile_marker = tmp_path / "first_request"
    runner.profile_targets = ("server",) if selected else ()
    popen = Mock()
    monkeypatch.setattr(aisbench.subprocess, "Popen", popen)
    runner._run_aisbench_task()
    env = popen.call_args.kwargs["env"]
    assert env["ASCEND_PROFILE_REQUEST_MARKER"] == str(runner.profile_marker)
    assert env["ASCEND_PROFILE_ENABLED"] == str(selected).lower()
    assert str(Path(aisbench.__file__).resolve().parents[1]) in env["PYTHONPATH"].split(aisbench.os.pathsep)


@pytest.mark.parametrize("stage", ["setup", "manifest", "empty", "unsupported", "marker", "start", "collect"])
def test_selected_profile_failure_fails_benchmark(tmp_path, benchmark_setup, monkeypatch, stage):
    config, controller, collect = benchmark_setup
    if stage == "setup":
        monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "invalid")
    elif stage == "manifest":
        (tmp_path / "serve_manifest.json").unlink()
    elif stage == "empty":
        capture.ServeManifest(()).write(tmp_path / "serve_manifest.json")
    elif stage == "unsupported":
        (tmp_path / "dataset.py").write_text("inferencer = dict(type=OtherInferencer)")
    elif stage == "marker":
        (tmp_path / "steady_state").write_text("not a directory")
    elif stage == "start":
        controller.start.side_effect = OSError("start failed")
    else:
        collect.side_effect = OSError("collection failed")
    completed = Mock()
    monkeypatch.setattr(aisbench.AisbenchRunner, "_wait_for_task", completed)
    with pytest.raises((ValueError, OSError, capture.ArtifactError)):
        aisbench.AisbenchRunner("model", 8000, config, verify=False)
    if stage == "collect":
        completed.assert_called_once()
    else:
        completed.assert_not_called()


@pytest.mark.parametrize("failure", ["unsupported", "marker"])
def test_timing_setup_failure_does_not_fail_ordinary_benchmark(tmp_path, benchmark_setup, monkeypatch, failure):
    config, controller, collect = benchmark_setup
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "false")
    if failure == "unsupported":
        (tmp_path / "dataset.py").write_text("inferencer = dict(type=OtherInferencer)")
    else:
        (tmp_path / "steady_state").write_text("not a directory")
    runner = aisbench.AisbenchRunner("model", 8000, config, verify=False)
    assert runner.profile_marker is None
    controller.start.assert_not_called()
    collect.assert_not_called()


@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("first_fails", [False, True])
def test_graph_and_capture_share_formal_request_origin(
    tmp_path, benchmark_setup, timed_inferencer, monkeypatch, profile, first_fails
):
    config, controller, _ = benchmark_setup
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", str(profile))
    clock = Mock(return_value=10.0)
    monkeypatch.setattr(capture, "time", types.SimpleNamespace(perf_counter=clock))

    def run(self):
        monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(self.profile_marker))
        # AISBench uses a separate inferencer for warmup. Long warmup must not
        # shift the graph or start the capture timer.
        asyncio.run(timed_inferencer().warmup(["warmup"], warmup_times=2))
        assert not self.profile_marker.exists()
        clock.return_value = 1000.0
        inferencer = timed_inferencer()
        if first_fails:
            with pytest.raises(ValueError, match="inference failed"):
                asyncio.run(inferencer.do_request("error", None, None))
            clock.return_value += 20
        asyncio.run(inferencer.do_request("formal", None, None))

    monkeypatch.setattr(aisbench.AisbenchRunner, "_run_aisbench_task", run)
    runner = aisbench.AisbenchRunner("model", 8000, config, verify=False)
    assert float(runner.profile_marker.read_text()) == 1000
    if profile:
        assert aisbench.ProfileController.call_args.args[2] == runner.profile_marker
    else:
        controller.start.assert_not_called()
    start = clock.return_value
    records = [
        {"time_points": [start, start + 30], "success": True},
        {"time_points": [start + 5, start + 25], "success": True},
    ]
    if first_fails:
        records.insert(0, {"time_points": [1000, 1005], "success": False})
    (tmp_path / "dataset_details.jsonl").write_text("\n".join(json.dumps(record) for record in records))
    runner.performance_result_dir = tmp_path
    runner.performance_dataset_type = "dataset"
    emit = Mock()
    monkeypatch.setattr(runner, "_emit_steady_state", emit)
    runner._try_analyze_steady_state()
    result = emit.call_args.args[0]
    offset = 20 if first_fails else 0
    assert (result.steady_start_s, result.steady_end_s, result.steady_duration_s) == (offset + 5, offset + 25, 20)
    assert result.timeline[0].time_s == 0
    assert result.timeline[0].running_requests == 0
    assert result.timeline[1].time_s == offset
    assert result.timeline[-1].time_s == offset + 30


@pytest.mark.parametrize("throughput", [5, 10])
def test_steady_analysis_failure_keeps_performance_verdict(monkeypatch, throughput):
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.result_json = {"Output Token Throughput": {"total": f"{throughput}token/s"}}
    runner.baseline, runner.threshold = 10, 1
    runner.input_throughput_threshold = runner.tpot_threshold = None
    monkeypatch.setattr(runner, "_get_result_performance", lambda: None)
    monkeypatch.setattr(runner, "_try_analyze_steady_state", Mock(side_effect=RuntimeError("analysis failed")))
    if throughput < 10:
        with pytest.raises(AssertionError, match="Performance verification failed"):
            runner._performance_verify()
    else:
        runner._performance_verify()


@pytest.mark.parametrize("save_fails", [False, True])
def test_steady_summary_and_console_output(tmp_path, monkeypatch, capsys, save_fails):
    monkeypatch.setattr(aisbench, "STEADY_STATE_OUTPUT_DIR", tmp_path)
    runner = aisbench.AisbenchRunner.__new__(aisbench.AisbenchRunner)
    runner.case_name, runner.profile_case_name = "load", "model__load"
    runner.performance_result_dir, runner.request_rate = None, 0
    output = tmp_path / "model__load"
    if save_fails:
        output.write_text("not a directory")
    runner._emit_steady_state(unavailable_steady_state(target_concurrency=2, reason="no timings"), None)
    assert "UNAVAILABLE" in capsys.readouterr().out
    if not save_fails:
        assert json.loads((output / "summary.json").read_text())["status"] == "unavailable"


@pytest.fixture
def timed_inferencer(monkeypatch):
    class Inferencer:
        async def warmup(self, data_list, warmup_times=1, concurrency=1):
            return await asyncio.gather(*(self.do_request(data_list[0], None, None) for _ in range(warmup_times)))

        async def do_request(self, data, token_bucket, session):
            if data == "error":
                raise ValueError("inference failed")
            return data

    package = "ais_bench.benchmark.openicl.icl_inferencer"
    for name in ("ais_bench", "ais_bench.benchmark", "ais_bench.benchmark.openicl", package):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules[package].GenInferencer = Inferencer
    name = "tools.profiling.aisbench_request_marker"
    spec = importlib.util.spec_from_file_location(name, Path(capture.__file__).with_name("aisbench_request_marker.py"))
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module.TimedGenInferencer


@pytest.mark.parametrize("marker_fails", [False, True])
@pytest.mark.parametrize("profile", [False, True])
@pytest.mark.parametrize("warmup_times", [0, 2])
def test_request_adapter_delegates_inference(
    monkeypatch, tmp_path, timed_inferencer, marker_fails, profile, warmup_times
):
    marker = Mock(side_effect=OSError("marker failed") if marker_fails else None)
    monkeypatch.setattr(sys.modules[timed_inferencer.__module__], "record_first_request", marker)
    path = tmp_path / "first_request"
    monkeypatch.setenv("ASCEND_PROFILE_REQUEST_MARKER", str(path))
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", str(profile))
    inferencer = timed_inferencer()
    assert asyncio.run(inferencer.warmup(["warmup"], warmup_times=warmup_times)) == ["warmup"] * warmup_times
    marker.assert_not_called()
    if marker_fails and profile:
        with pytest.raises(OSError, match="marker failed"):
            asyncio.run(inferencer.do_request("first", None, None))
        return
    for data in ("first", "second"):
        assert asyncio.run(inferencer.do_request(data, None, None)) == data
    with pytest.raises(ValueError, match="inference failed"):
        asyncio.run(inferencer.do_request("error", None, None))
    marker.assert_called_once_with(path)
