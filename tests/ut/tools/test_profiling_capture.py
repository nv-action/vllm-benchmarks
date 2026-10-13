"""Configuration, serving targets, and request-triggered capture lifecycle."""

import importlib.util
import json
import shlex
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tests.e2e.common.multi_node.internal_dp.multi_node_config import DisaggregatedPrefillCfg, NodeInfo
from tools.profiling import capture


def test_profile_options_and_environment(monkeypatch):
    for name in capture.ProfileSpec().environment():
        monkeypatch.delenv(name, raising=False)
    assert capture.ProfileSpec.from_env() == capture.ProfileSpec()
    spec = capture.ProfileSpec.from_options(
        {"enabled": "True", "with_stack": "FALSE", "start_after": "0", "duration": "12", "cases": "small, large,,"}
    )
    assert spec == capture.ProfileSpec(enabled=True, start_after=0, duration=12, cases=("small", "large"))
    for name, value in spec.environment().items():
        monkeypatch.setenv(name, value)
    assert capture.ProfileSpec.from_env() == spec
    assert capture.ProfileSpec.from_options(spec.options()) == spec
    assert spec.includes_case("small", "performance")
    assert not spec.includes_case("small", "accuracy")
    assert not spec.includes_case("other", "performance")
    assert capture.ProfileSpec(enabled=True).includes_case("other", "performance")
    assert not capture.ProfileSpec().includes_case("small", "performance")


@pytest.mark.parametrize(
    "options",
    [
        {"enabled": "yes"},
        {"start_after": -1},
        {"duration": 0},
        {"duration": 1.5},
        {"scope": "all"},
        {"scope": "representative"},
        {"cases": []},
    ],
)
def test_invalid_profile_options(options):
    with pytest.raises(ValueError):
        capture.ProfileSpec.from_options(options)


@pytest.mark.parametrize("output", ["raw", "parsed"])
def test_configure_cli_emits_ci_options(tmp_path, monkeypatch, output):
    outputs, env_file, env_json = (tmp_path / name for name in ("outputs", "env", "env.json"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "configure",
            "--options-json",
            json.dumps({"enabled": True, "output": output}),
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


@pytest.mark.parametrize("roles", [("unified",), ("prefill", "decode")])
def test_serving_manifest_preserves_all_instances(tmp_path, roles):
    instances = tuple(
        capture.ServeInstance.from_endpoint(f"{role}-{rank}", "http://server:8000", node_index=rank, root=tmp_path)
        for role in roles
        for rank in (1, 0)
    )
    manifest = capture.ServeManifest(instances)
    path = tmp_path / "serve.json"
    manifest.write(path)
    restored = capture.ServeManifest.read(path)
    assert restored == manifest
    assert restored.instances == instances


@pytest.mark.parametrize("as_shell", [False, True])
def test_profiler_command_merges_settings(tmp_path, as_shell):
    instance = capture.ServeInstance.from_endpoint("server/rank0", "http://server:8000", root=tmp_path)
    command = ["vllm", "serve", "model with spaces", "--profiler-config", '{"torch_profiler_with_memory":true}']
    configured = capture.configure_profiler_command(
        shlex.join(command) if as_shell else command, instance, capture.ProfileSpec(with_stack=True)
    )
    args = shlex.split(configured) if as_shell else configured
    assert args[:3] == command[:3]
    assert args.count("--profiler-config") == 1
    config = json.loads(args[args.index("--profiler-config") + 1])
    assert config["profiler"] == "torch"
    assert config["torch_profiler_with_memory"] is True
    assert config["torch_profiler_with_stack"] is True
    assert config["torch_profiler_dir"] == str(tmp_path / "raw" / "server_rank0")


def test_first_request_marker_is_write_once(tmp_path, monkeypatch):
    monkeypatch.setattr(capture, "time", SimpleNamespace(perf_counter=Mock(side_effect=[10.0, 20.0])))
    marker = tmp_path / "request"
    capture.record_first_request(marker)
    capture.record_first_request(marker)
    assert float(marker.read_text()) == 10.0


class CaptureClock:
    """Advance the controller's waits without sleeping or launching its thread."""

    def __init__(self, finish_at=None):
        self.now = 0
        self.finish_at = finish_at

    def wait(self, seconds):
        self.now += seconds
        if self.finish_at is not None:
            self.now = min(self.now, self.finish_at)
        return self.is_set()

    def is_set(self):
        return self.finish_at is not None and self.now >= self.finish_at


@pytest.mark.parametrize(
    "failure,status,reason",
    [
        (None, "success", "duration_reached"),
        ("start", "partial", "start_failed"),
        ("stop", "partial", "duration_reached"),
        ("early_end", "partial", "benchmark_ended"),
    ],
)
def test_capture_window_stops_every_target(tmp_path, monkeypatch, failure, status, reason):
    clock = CaptureClock(finish_at=3 if failure == "early_end" else None)
    monkeypatch.setattr(capture, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    marker = tmp_path / "request"
    marker.write_text("0")
    targets = tuple(
        capture.ServeInstance.from_endpoint(f"rank-{rank}", "http://server:8000", root=tmp_path) for rank in (0, 1)
    )
    client = Mock()
    if failure in ("start", "stop"):
        getattr(client, f"{failure}_profile").side_effect = OSError("request failed")
    controller = capture.ProfileController(capture.ProfileSpec(start_after=2, duration=4), targets, marker, client)
    controller.finished = clock
    controller._run_profile_window()
    assert controller.result["status"] == status
    assert controller.result["reason"] == reason
    assert set(controller.result["targets"]) == {"rank-0", "rank-1"}
    client.start_profile.assert_called_once_with("http://server:8000")
    client.stop_profile.assert_called_once_with("http://server:8000")
    if failure is None:
        assert controller.result["actual_duration"] == 4


def test_selected_capture_without_a_request_is_incomplete(tmp_path):
    client = Mock()
    controller = capture.ProfileController(capture.ProfileSpec(), (), tmp_path / "request", client)
    controller.start()
    assert controller.finish()["status"] == "partial"
    client.start_profile.assert_not_called()
    client.stop_profile.assert_not_called()


def test_benchmark_ending_before_capture_is_incomplete(tmp_path, monkeypatch):
    marker = tmp_path / "request"
    marker.write_text("10")
    clock = CaptureClock(finish_at=12)
    monkeypatch.setattr(capture, "time", SimpleNamespace(perf_counter=lambda: clock.now))
    client = Mock()
    controller = capture.ProfileController(capture.ProfileSpec(start_after=5), (), marker, client)
    controller.finished = clock
    controller._run_profile_window()
    assert controller.result["status"] == "partial"
    assert controller.result["reason"] == "benchmark_ended_before_start"
    client.start_profile.assert_not_called()


def test_capture_shutdown_timeout_does_not_allow_collection(tmp_path):
    controller = capture.ProfileController(capture.ProfileSpec(), (), tmp_path / "request")
    controller.thread = Mock()
    controller.thread.is_alive.return_value = True
    with pytest.raises(capture.ArtifactError, match="traces are still in use"):
        controller.finish()


@pytest.mark.parametrize("headless", [(False, True), (False, False, False, True), (False, True, False, True)])
def test_internal_dp_configures_all_nodes_and_controls_unique_endpoints(tmp_path, monkeypatch, pytestconfig, headless):
    # Only the server runtime is unavailable locally; load the actual deployment helper and config classes.
    monkeypatch.setitem(sys.modules, "vllm", ModuleType("vllm"))
    conftest = ModuleType("tests.e2e.conftest")
    conftest.RemoteOpenAIServer = Mock()
    monkeypatch.setitem(sys.modules, "tests.e2e.conftest", conftest)
    pytestconfig.addinivalue_line("markers", "asyncio: asynchronous server test")
    path = Path(capture.__file__).resolve().parents[2] / "tests/e2e/common/multi_node/internal_dp/test_multi_node.py"
    spec = importlib.util.spec_from_file_location("profiling_internal_dp_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("ASCEND_PROFILE_ROOT", str(tmp_path))
    nodes = [
        NodeInfo(index, f"host-{index}", "serve model", {"SERVER_PORT": 8000 + index}, is_headless)
        for index, is_headless in enumerate(headless)
    ]
    disagg = (
        DisaggregatedPrefillCfg({"prefiller_host_index": [0, 1], "decoder_host_index": [2, 3]}, 4)
        if len(nodes) == 4
        else None
    )
    for node in nodes:
        config = SimpleNamespace(
            nodes=nodes,
            cur_node=node,
            is_master=node.index == 0,
            master_ip=nodes[0].ip,
            disagg_cfg=disagg,
            server_cmd=node.server_cmd,
        )
        module._configure_profiling(config, capture.ProfileSpec(enabled=True))
        command = shlex.split(config.server_cmd)
        profiler = json.loads(command[command.index("--profiler-config") + 1])
        role = "prefill" if node.index < 2 else "decode"
        name = f"{role}-{node.index % 2}" if disagg else f"dp-{node.index}"
        assert profiler["torch_profiler_dir"] == str(tmp_path / "raw" / name)

    instances = capture.ServeManifest.read(tmp_path / "serve_manifest.json").instances
    assert [instance.node_index for instance in instances] == list(range(len(nodes)))
    assert len({instance.raw_trace_dir for instance in instances}) == len(nodes)
    for node, instance in zip(nodes, instances):
        controller_index = node.index
        if node.headless:
            controller_index = 2 if disagg and node.index >= 2 else 0
        assert instance.endpoint == f"http://host-{controller_index}:{8000 + controller_index}"
    client = Mock()
    controller = capture.ProfileController(capture.ProfileSpec(), instances, tmp_path / "request", client)
    controller._call_profile_endpoints(client.start_profile, instances)
    controller._call_profile_endpoints(client.stop_profile, instances)
    endpoints = {instance.endpoint for instance in instances}
    assert {call.args[0] for call in client.start_profile.call_args_list} == endpoints
    assert client.start_profile.call_count == client.stop_profile.call_count == len(endpoints)
