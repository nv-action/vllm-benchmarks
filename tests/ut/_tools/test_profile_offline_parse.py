import io
import json
import sys
import tarfile
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from tools import profile


class FakeObs:
    def __init__(self):
        self.objects = {}
        self.uploads = []

    def upload_file(self, source, bucket, key, **kwargs):
        data = Path(source).read_bytes()
        self.objects[bucket, key] = data
        self.uploads.append(key)
        if callback := kwargs.get("Callback"):
            callback(len(data))

    def download_file(self, bucket, key, target, **kwargs):
        if (bucket, key) not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "missing"}}, "GetObject")
        data = self.objects[bucket, key]
        Path(target).write_bytes(data)
        if callback := kwargs.get("Callback"):
            callback(len(data))

    def head_object(self, Bucket, Key):
        return {"ContentLength": len(self.objects[Bucket, Key])}


@pytest.fixture
def fake_obs(monkeypatch):
    import boto3

    client = FakeObs()
    monkeypatch.setattr(boto3, "client", lambda *_, **__: client)
    return client


def _upload_raw(tmp_path, fake_obs, monkeypatch):
    monkeypatch.setenv("NIGHTLY_PROFILE_ENABLED", "true")
    collector = tmp_path / "collector"
    raw_dir = collector / "raw" / "serve_0"
    for name in ("worker_0_ascend_pt", "worker_1_ascend_pt"):
        trace = raw_dir / name
        trace.mkdir(parents=True)
        (trace / "raw.bin").write_bytes(b"raw trace")
        (trace / "profiler_info.json").write_text("{}")
    target = profile.ServeInstance("serve_0", "http://localhost", str(raw_dir))
    profile.ArtifactManager(collector, "parsed").collect("perf", (target,), {"status": "success"})
    profile.upload_artifacts(collector, "nightly/run", "bucket", "endpoint", "region")
    return dict(fake_obs.objects)


def _mock_analyse(monkeypatch, parsed_json="{}"):
    calls = []

    def analyse(path, *, max_process_number):
        calls.append((Path(path).name, max_process_number))
        output = Path(path) / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        (output / "analyse.done").write_text("done")
        (output / "trace_view.json").write_text(parsed_json)

    profiler = types.ModuleType("torch_npu.profiler.profiler")
    profiler.analyse = analyse
    monkeypatch.setitem(sys.modules, "torch_npu", types.ModuleType("torch_npu"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler", types.ModuleType("torch_npu.profiler"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler.profiler", profiler)
    return calls


def test_offline_parse_uploads_parsed_manifest_last(tmp_path, fake_obs, monkeypatch):
    raw_objects = _upload_raw(tmp_path, fake_obs, monkeypatch)
    calls = _mock_analyse(monkeypatch)

    manifest = profile.parse_artifacts(tmp_path / "parser", "nightly/run", "bucket", "endpoint", "region")

    assert calls == [("worker_0_ascend_pt", 16), ("worker_1_ascend_pt", 16)]
    assert manifest["status"] == "success"
    record = manifest["cases"][0]["targets"][0]
    assert record["output"] == "parsed"
    assert record["raw_obs_url"] == "obs://bucket/nightly/run/raw/perf/serve_0.tar.gz"
    assert record["obs_url"] == "obs://bucket/nightly/run/parsed/perf/serve_0.tar.gz"
    assert fake_obs.uploads[-1] == "nightly/run/parsed/profile_manifest.json"
    assert all(fake_obs.objects[key] == value for key, value in raw_objects.items())
    archive = fake_obs.objects["bucket", "nightly/run/parsed/perf/serve_0.tar.gz"]
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = tar.getnames()
    assert sum(name.endswith("trace_view.json") for name in names) == 2
    assert not any(name.endswith("raw.bin") for name in names)


def test_offline_parse_invalid_trace_is_reported_and_raw_is_kept(tmp_path, fake_obs, monkeypatch):
    raw_objects = _upload_raw(tmp_path, fake_obs, monkeypatch)
    _mock_analyse(monkeypatch, parsed_json="invalid json")

    with pytest.raises(RuntimeError, match="Offline profiling incomplete"):
        profile.parse_artifacts(tmp_path / "parser", "nightly/run", "bucket", "endpoint", "region", 4)

    manifest = json.loads(fake_obs.objects["bucket", "nightly/run/parsed/profile_manifest.json"])
    assert manifest["status"] == "partial"
    assert "parse_error" in manifest["cases"][0]["targets"][0]
    assert all(fake_obs.objects[key] == value for key, value in raw_objects.items())


def test_offline_parse_marks_partial_raw_manifest_as_failed(tmp_path, fake_obs, monkeypatch):
    _upload_raw(tmp_path, fake_obs, monkeypatch)
    _mock_analyse(monkeypatch)
    key = "bucket", "nightly/run/raw/profile_manifest.json"
    manifest = json.loads(fake_obs.objects[key])
    manifest["status"] = "partial"
    fake_obs.objects[key] = json.dumps(manifest).encode()

    with pytest.raises(RuntimeError, match="raw/parsed status=partial"):
        profile.parse_artifacts(tmp_path / "parser", "nightly/run", "bucket", "endpoint", "region")

    parsed = json.loads(fake_obs.objects["bucket", "nightly/run/parsed/profile_manifest.json"])
    assert parsed["status"] == "partial"


def test_offline_parse_skips_only_missing_raw_manifest(tmp_path, fake_obs):
    assert profile.parse_artifacts(tmp_path, "nightly/run", "bucket", "endpoint", "region") == {
        "status": "skipped",
        "reason": "raw_manifest_missing",
    }


def test_offline_parse_does_not_skip_permission_error(tmp_path, fake_obs):
    def denied(*_, **__):
        raise ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "GetObject")

    fake_obs.download_file = denied
    with pytest.raises(ClientError):
        profile.parse_artifacts(tmp_path, "nightly/run", "bucket", "endpoint", "region")


def test_upload_command_fails_on_partial_manifest(tmp_path, monkeypatch):
    monkeypatch.setattr(profile, "upload_artifacts", lambda *_, **__: {"status": "partial"})
    monkeypatch.setattr(sys, "argv", ["profile", "upload", "--root", str(tmp_path), "--prefix", "nightly/run"])
    with pytest.raises(RuntimeError, match="raw artifact/upload incomplete"):
        profile.main()


def test_obs_capacity_reports_estimated_remaining(monkeypatch, capsys):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "ak")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "sk")
    client = SimpleNamespace(
        getBucketStorageInfo=lambda _: SimpleNamespace(status=200, body=SimpleNamespace(size=100)),
        getBucketQuota=lambda _: SimpleNamespace(status=200, body=SimpleNamespace(quota=200)),
        close=lambda: None,
    )
    obs = types.ModuleType("obs")
    obs.ObsClient = lambda **_: client
    monkeypatch.setitem(sys.modules, "obs", obs)

    profile.log_obs_storage("bucket", "endpoint")

    assert "remaining=100 bytes (estimated)" in capsys.readouterr().out
