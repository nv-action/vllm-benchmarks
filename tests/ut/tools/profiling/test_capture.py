"""Configuration, serving topology, and runtime capture behavior."""

import json
import shlex
import sys
import time
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, call

import pytest

from tools.profiling import capture


@pytest.fixture(autouse=True)
def profile_environment(monkeypatch):
    for name in capture.ProfileSpec().environment():
        monkeypatch.delenv(name, raising=False)


def test_profile_configuration_round_trip(monkeypatch):
    assert capture.ProfileSpec.from_env() == capture.ProfileSpec(enabled=False)
    options = {
        "enabled": "TRUE",
        "with_stack": "false",
        "start_after": "50",
        "duration": 8,
        "output": "raw",
        "benchmark_names": " throughput, latency ",
    }
    spec = capture.ProfileSpec.from_options(options)
    assert (spec.enabled, spec.with_stack, spec.start_after, spec.duration, spec.output) == (True, False, 50, 8, "raw")
    assert spec.benchmark_names == ("throughput", "latency")
    for name, value in spec.environment().items():
        monkeypatch.setenv(name, value)
    assert capture.ProfileSpec.from_env() == capture.ProfileSpec.from_options(spec.options()) == spec


@pytest.mark.parametrize(
    "options",
    [
        {"enabled": "yes"},
        {"duration": 0},
        {"start_after": -1},
        {"duration": 1.5},
        {"output": "invalid"},
        {"benchmark_names": ["throughput"]},
        {"unexpected_option": True},
    ],
)
def test_invalid_user_configuration_is_rejected(options):
    with pytest.raises(ValueError):
        capture.ProfileSpec.from_options(options)


@pytest.mark.parametrize(
    "enabled,names,benchmark,kind,selected",
    [
        (False, (), "throughput", "performance", False),
        (True, (), "throughput", "performance", True),
        (True, ("throughput",), "throughput", "performance", True),
        (True, ("throughput",), "latency", "performance", False),
        (True, (), "accuracy", "accuracy", False),
    ],
)
def test_capture_selection(enabled, names, benchmark, kind, selected):
    spec = capture.ProfileSpec(enabled=enabled, benchmark_names=names)
    assert spec.includes_benchmark(benchmark, kind) is selected


@pytest.mark.parametrize("output", ["raw", "parsed"])
def test_configure_cli_produces_workflow_outputs(tmp_path, monkeypatch, output):
    outputs, env_file, env_json = (tmp_path / name for name in ("outputs", "env", "env.json"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "configure",
            "--options-json",
            json.dumps({"enabled": True, "output": output, "benchmark_names": "throughput"}),
            "--output",
            str(outputs),
            "--env-output",
            str(env_file),
            "--env-json",
            str(env_json),
        ],
    )
    capture.main()
    values = dict(line.split("=", 1) for line in outputs.read_text().splitlines())
    assert values["enabled"] == "true"
    assert values["parse"] == str(output == "parsed").lower()
    spec = capture.ProfileSpec.from_options(json.loads(values["value"]))
    environment = dict(line.split("=", 1) for line in env_file.read_text().splitlines())
    assert environment == spec.environment() == json.loads(env_json.read_text())["profile_env"]


@pytest.fixture
def serving_instances(tmp_path):
    return (
        capture.ServeInstance.from_endpoint("dp-0", "http://leader:8000", root=tmp_path),
        capture.ServeInstance.from_endpoint("dp-1", "http://leader:8000", node_index=1, root=tmp_path),
        capture.ServeInstance.from_endpoint("decode", "http://decoder:8000", node_index=1, root=tmp_path),
    )


@pytest.mark.parametrize("shell_command", [False, True])
def test_all_serving_instances_receive_independent_trace_directories(tmp_path, serving_instances, shell_command):
    path = tmp_path / "serve_manifest.json"
    capture.ServeManifest(serving_instances).write(path)
    instances = capture.ServeManifest.read(path).instances
    assert instances == serving_instances
    command = ["vllm", "serve", "model with spaces", "--profiler-config", '{"torch_profiler_with_memory":true}']
    original = shlex.join(command) if shell_command else command
    directories = []
    for instance in instances:
        configured = capture.configure_profiler_command(original, instance, capture.ProfileSpec(with_stack=True))
        args = shlex.split(configured) if shell_command else configured
        assert args[:3] == command[:3]
        assert args.count("--profiler-config") == 1
        options = json.loads(args[args.index("--profiler-config") + 1])
        assert options["profiler"] == "torch"
        assert options["torch_profiler_with_memory"] is options["torch_profiler_with_stack"] is True
        directories.append(options["torch_profiler_dir"])
    assert len(set(directories)) == len(instances)
    assert command[-1] == '{"torch_profiler_with_memory":true}'


@pytest.mark.parametrize("outcome", ["complete", "start_error", "stop_error", "benchmark_ended"])
def test_capture_lifecycle_controls_each_api_once(tmp_path, monkeypatch, serving_instances, outcome):
    monkeypatch.setattr(capture, "POLL_INTERVAL_SECONDS", 0.001)
    marker = tmp_path / "first_request"
    capture.record_first_request(marker)
    started, stopped = Event(), Event()

    def start(endpoint):
        started.set()
        if outcome == "start_error" and endpoint == "http://leader:8000":
            raise OSError("start unavailable")

    def stop(endpoint):
        stopped.set()
        if outcome == "stop_error" and endpoint == "http://leader:8000":
            raise OSError("stop unavailable")

    client = SimpleNamespace(start_profile=Mock(side_effect=start), stop_profile=Mock(side_effect=stop))
    controller = capture.ProfileController(
        capture.ProfileSpec(start_after=0, duration=1), serving_instances, marker, client
    )
    controller.start()
    try:
        assert started.wait(5)
        if outcome != "benchmark_ended":
            assert stopped.wait(5)
    finally:
        result = controller.finish()
    endpoints = [call("http://leader:8000"), call("http://decoder:8000")]
    assert client.start_profile.call_count == client.stop_profile.call_count == 2
    client.start_profile.assert_has_calls(endpoints, any_order=True)
    client.stop_profile.assert_has_calls(endpoints, any_order=True)
    assert result["status"] == ("success" if outcome == "complete" else "partial")
    if outcome == "complete":
        assert result["actual_duration"] >= 1
    elif outcome in ("start_error", "stop_error"):
        assert result["targets"]["dp-0"][outcome] == result["targets"]["dp-1"][outcome]
        assert "unavailable" in result["targets"]["dp-0"][outcome]


@pytest.mark.parametrize("has_request", [False, True])
def test_benchmark_finishing_before_capture_is_partial(tmp_path, monkeypatch, serving_instances, has_request):
    monkeypatch.setattr(capture, "POLL_INTERVAL_SECONDS", 0.001)
    marker = tmp_path / "first_request"
    if has_request:
        marker.write_text(str(time.perf_counter()))
    client = Mock()
    controller = capture.ProfileController(capture.ProfileSpec(start_after=60), serving_instances, marker, client)
    controller.start()
    result = controller.finish()
    assert result["status"] == "partial"
    assert result["reason"] in ("no_request_before_benchmark_end", "benchmark_ended_before_start")
    client.start_profile.assert_not_called()
    client.stop_profile.assert_not_called()


def test_capture_waits_for_the_offset_from_the_first_request(tmp_path, monkeypatch, serving_instances):
    monkeypatch.setattr(capture, "POLL_INTERVAL_SECONDS", 0.001)
    marker = tmp_path / "first_request"
    started, timestamps = Event(), []

    def start(endpoint):
        timestamps.append(time.perf_counter())
        started.set()

    client = SimpleNamespace(start_profile=start, stop_profile=Mock())
    controller = capture.ProfileController(capture.ProfileSpec(start_after=1), serving_instances[:1], marker, client)
    controller.start()
    try:
        assert not started.wait(0.01)
        capture.record_first_request(marker)
        assert started.wait(5)
    finally:
        controller.finish()
    assert timestamps[0] >= float(marker.read_text()) + 1
    client.stop_profile.assert_called_once_with("http://leader:8000")
