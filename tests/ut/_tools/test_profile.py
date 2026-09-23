import asyncio
import json
import tarfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml

from tools import profile


def test_spec_defaults_and_case_filter(monkeypatch):
    monkeypatch.delenv("NIGHTLY_PROFILE_ENABLED", raising=False)
    assert profile.ProfileSpec.from_env() == profile.ProfileSpec()
    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    monkeypatch.setenv("NIGHTLY_PROFILE_CASES", "perf,perf_long")
    spec = profile.ProfileSpec.from_env()
    assert (spec.start_after, spec.duration, spec.max_size_bytes) == (15, 8, 50 * 1024**3)
    assert spec.includes("perf", "performance")
    assert not spec.includes("other", "performance")
    assert not spec.includes("perf", "accuracy")


@pytest.mark.parametrize("value", ["oops", "0G", "-1G"])
def test_invalid_size(monkeypatch, value):
    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    monkeypatch.setenv("NIGHTLY_PROFILE_MAX_SIZE", value)
    with pytest.raises(ValueError):
        profile.ProfileSpec.from_env()


def test_manifest_selector_and_config(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTLY_PROFILE_ROOT", str(tmp_path))
    instances = [
        profile.make_instance("prefill-1", "http://a", "prefill", 1),
        profile.make_instance("decode-1", "http://b", "decode", 1),
        profile.make_instance("prefill-0", "http://c", "prefill", 0),
        profile.make_instance("decode-0", "http://d", "decode", 0),
    ]
    profile.install_manifest(instances)
    manifest = profile.ServeManifest.read(tmp_path / "serve_manifest.json")
    assert [i.name for i in profile.TargetSelector.select(manifest, "representative")] == ["prefill-0", "decode-0"]
    assert profile.TargetSelector.select(manifest, "all") == tuple(instances)
    dp = profile.ServeManifest(tuple(profile.make_instance(f"dp-{n}", "http://a", dp_rank=n) for n in (2, 0, 1)))
    assert profile.TargetSelector.select(dp, "representative")[0].dp_rank == 0
    args = profile.with_profiler_config(
        ["--foo", "bar", "--profiler-config", '{"custom":"keep"}'], instances[0], profile.ProfileSpec(with_stack=True)
    )
    config = json.loads(args[args.index("--profiler-config") + 1])
    assert config["custom"] == "keep"
    assert config["torch_profiler_dir"] == instances[0].profile_dir
    assert config["torch_profiler_with_stack"] is True


def test_first_request_marker(tmp_path):
    marker = tmp_path / "first"

    class Base:
        async def stream_infer(self, *_):
            return "stream"

        async def text_infer(self, *_):
            return "text"

    model = profile.profiled_model(Base, str(marker))()
    assert asyncio.run(model.stream_infer({}, None)) == "stream"
    first = float(marker.read_text())
    assert asyncio.run(model.text_infer({}, None)) == "text"
    assert float(marker.read_text()) == first


class Client:
    def __init__(self, failures=()):
        self.started = []
        self.stopped = []
        self.failures = failures
        self.done = threading.Event()

    def start_profile(self, endpoint):
        self.started.append(endpoint)
        if endpoint in self.failures:
            raise RuntimeError("start failed")

    def stop_profile(self, endpoint):
        self.stopped.append(endpoint)
        self.done.set()


def test_controller_parallel_partial_and_size(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "POLL_INTERVAL", 0.005)
    targets = tuple(profile.make_instance(name, f"http://{name}") for name in ("one", "two"))
    marker = tmp_path / "first"
    profile._mark_first_request(str(marker))
    client = Client(failures=("http://two",))
    controller = profile.ProfileController(
        profile.ProfileSpec(enabled=True, start_after=0, duration=0.02), targets, marker, client
    )
    controller.start()
    assert client.done.wait(2)
    result = controller.finish()
    assert set(client.started) == {"http://one", "http://two"}
    assert set(client.stopped) == {"http://one", "http://two"}
    assert result["status"] == "partial"
    assert result["targets"]["two"]["start_error"] == "start failed"

    client = Client()
    monkeypatch.setattr(profile, "directory_size", lambda _: 10)
    controller = profile.ProfileController(
        profile.ProfileSpec(enabled=True, start_after=0, duration=10, max_size_bytes=1), targets[:1], marker, client
    )
    controller.start()
    assert client.done.wait(2)
    assert controller.finish()["reason"] == "size_limit_exceeded"


def test_controller_benchmark_ends_before_request(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "POLL_INTERVAL", 0.005)
    client = Client()
    controller = profile.ProfileController(profile.ProfileSpec(enabled=True), (), tmp_path / "missing", client)
    controller.start()
    assert controller.finish()["reason"] == "no_request_before_benchmark_end"


def test_controller_stop_timeout_is_partial(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "POLL_INTERVAL", 0.005)
    marker = tmp_path / "first"
    profile._mark_first_request(str(marker))

    class TimeoutClient(Client):
        def stop_profile(self, endpoint):
            self.done.set()
            raise TimeoutError("stop timed out")

    client = TimeoutClient()
    target = profile.make_instance("serve-0", "http://localhost")
    controller = profile.ProfileController(
        profile.ProfileSpec(enabled=True, start_after=0, duration=0.02), (target,), marker, client
    )
    controller.start()
    assert client.done.wait(2)
    result = controller.finish()
    assert result["status"] == "partial"
    assert result["targets"]["serve-0"]["stop_error"] == "stop timed out"


def test_controller_starts_and_stops_targets_concurrently(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "POLL_INTERVAL", 0.005)
    marker = tmp_path / "first"
    profile._mark_first_request(str(marker))
    start_barrier = threading.Barrier(2, timeout=1)
    stop_barrier = threading.Barrier(2, timeout=1)

    class BarrierClient:
        def start_profile(self, _):
            start_barrier.wait()

        def stop_profile(self, _):
            stop_barrier.wait()

    targets = tuple(profile.make_instance(f"serve-{i}", f"http://{i}") for i in range(2))
    controller = profile.ProfileController(
        profile.ProfileSpec(enabled=True, start_after=0, duration=0.02), targets, marker, BarrierClient()
    )
    controller.start()
    controller.thread.join(2)
    assert controller.finish()["status"] == "success"


def test_artifact_raw_fallback_and_manifest(tmp_path, monkeypatch):
    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    raw = tmp_path / "raw" / "serve-0"
    trace = raw / "worker_ascend_pt"
    trace.mkdir(parents=True)
    (trace / "data.bin").write_bytes(b"trace")
    target = profile.ServeInstance("serve-0", "http://localhost", str(raw))
    profile.ArtifactManager(tmp_path, "parsed").collect(
        "perf", (target,), {"status": "success", "actual_duration": 8, "targets": {"serve-0": {}}}
    )
    manifest = json.loads((tmp_path / "profile_manifest.json").read_text())
    record = manifest["cases"][0]["targets"][0]
    assert manifest["status"] == "partial"
    assert record["output"] == "raw-fallback"
    with tarfile.open(tmp_path / record["archive"]) as tar:
        assert "serve-0/worker_ascend_pt/data.bin" in tar.getnames()
    assert not list(raw.iterdir())


def test_artifact_parsed_only_and_multiple_cases(tmp_path, monkeypatch):
    import sys
    import types

    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    raw = tmp_path / "raw" / "serve-0"
    target = profile.ServeInstance("serve-0", "http://localhost", str(raw))

    def analyse(path, **_):
        parsed = Path(path) / "ASCEND_PROFILER_OUTPUT"
        parsed.mkdir()
        (parsed / "analyse.done").write_text("done")
        (parsed / "trace_view.json").write_text("{}")

    profiler = types.ModuleType("torch_npu.profiler.profiler")
    profiler.analyse = analyse
    monkeypatch.setitem(sys.modules, "torch_npu", types.ModuleType("torch_npu"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler", types.ModuleType("torch_npu.profiler"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler.profiler", profiler)
    manager = profile.ArtifactManager(tmp_path, "parsed")
    for case_name in ("perf", "perf_long"):
        trace = raw / "worker_ascend_pt"
        trace.mkdir(parents=True)
        (trace / "huge_raw.bin").write_bytes(b"raw")
        manager.collect(case_name, (target,), {"status": "success", "actual_duration": 8, "targets": {"serve-0": {}}})
    manifest = json.loads((tmp_path / "profile_manifest.json").read_text())
    assert [case["case"] for case in manifest["cases"]] == ["perf", "perf_long"]
    assert manifest["status"] == "success"
    for case in manifest["cases"]:
        record = case["targets"][0]
        assert record["output"] == "parsed"
        with tarfile.open(tmp_path / record["archive"]) as tar:
            names = tar.getnames()
            assert any(name.endswith("trace_view.json") for name in names)
            assert not any(name.endswith("huge_raw.bin") for name in names)


def test_upload_artifacts_verifies_each_object(tmp_path, monkeypatch):
    import boto3

    archive = tmp_path / "perf" / "serve-0.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"archive")
    manifest = {"status": "success", "cases": [{"targets": [{"archive": "perf/serve-0.tar.gz"}]}]}
    (tmp_path / "profile_manifest.json").write_text(json.dumps(manifest))
    lengths = {}

    def upload_file(source, bucket, key, **_):
        lengths[key] = Path(source).stat().st_size

    client = SimpleNamespace(
        upload_file=Mock(side_effect=upload_file), head_object=lambda Bucket, Key: {"ContentLength": lengths[Key]}
    )
    monkeypatch.setattr(boto3, "client", lambda *_, **__: client)
    result = profile.upload_artifacts(tmp_path, "nightly-profiling/run", "bucket", "endpoint", "region")
    assert result["cases"][0]["targets"][0]["obs_url"] == "obs://bucket/nightly-profiling/run/perf/serve-0.tar.gz"
    assert client.upload_file.call_count == 2


def test_aisbench_profiles_only_selected_performance_case(tmp_path, monkeypatch):
    from tools import aisbench

    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    monkeypatch.setenv("NIGHTLY_PROFILE_CASES", "perf")
    monkeypatch.setenv("NIGHTLY_PROFILE_ROOT", str(tmp_path))
    profile.install_manifest([profile.make_instance("serve-0", "http://localhost:8000")])
    events = []
    monkeypatch.setattr(aisbench.AisbenchRunner, "_init_dataset_conf", lambda self: None)
    monkeypatch.setattr(aisbench.AisbenchRunner, "_init_request_conf", lambda self: None)
    monkeypatch.setattr(aisbench.AisbenchRunner, "_run_aisbench_task", lambda self: events.append("benchmark"))
    monkeypatch.setattr(aisbench.AisbenchRunner, "_wait_for_task", lambda self: events.append("benchmark_done"))

    class FakeController:
        def __init__(self, *_):
            pass

        def start(self):
            events.append("profile_start")

        def finish(self):
            events.append("profile_finish")
            return {"status": "success", "targets": {}}

    monkeypatch.setattr(aisbench, "ProfileController", FakeController)
    monkeypatch.setattr(aisbench.ArtifactManager, "collect", lambda self, *_: events.append("collect"))
    case = {
        "case_name": "perf",
        "profile_case_name": "config-one__perf",
        "case_type": "performance",
        "dataset_path_local": "/dataset",
        "model_path": "/model",
        "dataset_conf": "dataset",
        "request_conf": "request",
        "max_out_len": 1,
        "batch_size": 1,
    }
    runner = aisbench.AisbenchRunner("model", 8000, case, verify=False)
    assert runner.profile_marker.name == "config-one__perf.first_request"
    assert events == ["profile_start", "benchmark", "benchmark_done", "profile_finish", "collect"]
    events.clear()
    aisbench.AisbenchRunner("model", 8000, {**case, "case_name": "other"}, verify=False)
    assert events == ["benchmark", "benchmark_done"]
    events.clear()
    (tmp_path / "serve_manifest.json").write_text("broken json")
    aisbench.AisbenchRunner("model", 8000, case, verify=False)
    assert events == ["benchmark", "benchmark_done"]


def test_a3_workflow_profile_plumbing():
    from jinja2 import Environment

    root = Path(__file__).resolve().parents[3]
    workflows = root / ".github" / "workflows"

    def workflow(name):
        return yaml.load((workflows / name).read_text(), Loader=yaml.BaseLoader)

    fields = (
        "profile_enabled",
        "profile_start_after",
        "profile_duration",
        "profile_with_stack",
        "profile_scope",
        "profile_max_size",
        "profile_output",
        "profile_cases",
    )
    schedule = workflow("schedule_nightly_test_a3.yaml")
    dispatch_inputs = schedule["on"]["workflow_dispatch"]["inputs"]
    assert len(dispatch_inputs) <= 10
    assert json.loads(dispatch_inputs["profile_options_json"]["default"]) == {}
    for job_name in ("multi-node-tests", "double-node-tests", "single-node-tests", "multi-card-tests"):
        assert all(field in schedule["jobs"][job_name]["with"] for field in fields)
    for name in ("_e2e_nightly_single_node.yaml", "_e2e_nightly_multi_node.yaml"):
        assert all(field in workflow(name)["on"]["workflow_call"]["inputs"] for field in fields)
    command = workflow("pr_nightly_command.yml")
    assert all(field in command["jobs"]["authorize"]["outputs"] for field in fields)
    assert "profile_options_json" in command["jobs"]["dispatch-a3"]["steps"][-1]["run"]

    template = (root / "tests/e2e/nightly/multi_node/scripts/lws.yaml.jinja2").read_text()
    rendered = Environment().from_string(template).render(log_prefix="/tmp/profile-test", profile_enabled="true")
    lws = next(yaml.safe_load_all(rendered))
    leader = lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["spec"]["containers"][0]
    env = {item["name"]: item["value"] for item in leader["env"]}
    assert env["NIGHTLY_PROFILE_ROOT"] == "/tmp/profile-test/profile_artifact"
    assert env["NIGHTLY_PROFILE_START_AFTER"] == "15"
    assert env["NIGHTLY_PROFILE_DURATION"] == "8"
