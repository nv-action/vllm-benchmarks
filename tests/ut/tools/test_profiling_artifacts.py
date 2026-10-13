"""Exercise real archives and manifests with in-memory OBS and a fake analyser."""

import io
import json
import sys
import tarfile
import types
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from tools.profiling import capture, parse, storage


class MemoryObs:
    """Implement only the OBS operations used by the artifact pipeline."""

    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.fail_upload = None

    def upload_file(self, source, bucket, key, Callback=None, **kwargs):
        if key == self.fail_upload:
            raise OSError("upload failed")
        data = Path(source).read_bytes()
        self.objects[bucket, key] = data
        self.uploads.append(key)
        if Callback:
            Callback(len(data))

    def download_file(self, bucket, key, destination, Callback=None, **kwargs):
        if (bucket, key) not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey"}}, "GetObject")
        data = self.objects[bucket, key]
        Path(destination).write_bytes(data)
        if Callback:
            Callback(len(data))


@pytest.fixture
def obs(monkeypatch):
    client = MemoryObs()
    for module in (capture, parse):
        monkeypatch.setattr(module, "create_obs_client", lambda *_: client)
        monkeypatch.setattr(module, "create_transfer_config", lambda: None)
    return client


@pytest.fixture
def raw_job(tmp_path):
    root = tmp_path / "capture"
    capture.initialize_profile_run(root, capture.ProfileSpec(enabled=True))
    targets = tuple(
        capture.ServeInstance.from_endpoint(f"worker-{node}", f"http://worker-{node}:8000", node_index=node, root=root)
        for node in (0, 1)
    )
    for target in targets:
        trace = Path(target.raw_trace_dir) / "rank_ascend_pt"
        trace.mkdir(parents=True)
        (trace / "raw.bin").write_bytes(b"raw profiler data")
        (trace / "profiler_info.json").write_text("{}")
    capture.collect_raw_artifacts(root, "load", targets, {"status": "success", "actual_duration": 8})
    return root, targets


@pytest.fixture
def rank_job(tmp_path, obs):
    target_dir = tmp_path / "work" / "worker"
    traces = [target_dir / f"rank-{index}_ascend_pt" for index in range(4)]
    for trace in traces:
        trace.mkdir(parents=True)
        (trace / "raw.bin").write_bytes(b"raw profiler data")

    def analyse(directory):
        output = Path(directory) / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        (output / "trace_view.json").write_text('{"traceEvents":[]}')
        (output / "analyse.done").touch()

    context = parse._ParseContext(
        root=tmp_path,
        raw_root=tmp_path / "raw",
        raw_prefix="job/raw",
        parsed_root=tmp_path / "parsed",
        parsed_prefix="job/parsed",
        bucket="bucket",
        node_index=0,
        analyse=analyse,
        download_client=obs,
        upload_client=obs,
        transfer_config=None,
    )
    return context, target_dir, traces


def test_collection_accumulates_cases_and_preserves_archived_data(raw_job):
    root, targets = raw_job
    first_manifest = json.loads((root / "profile_manifest.json").read_text())
    for target, record in zip(targets, first_manifest["cases"][0]["targets"]):
        assert not list(Path(target.raw_trace_dir).iterdir())
        with tarfile.open(root / record["archive"]) as archive:
            assert archive.extractfile(f"{target.name}/rank_ascend_pt/raw.bin").read() == b"raw profiler data"
        (Path(target.raw_trace_dir) / "next.bin").write_bytes(b"next case")
    capture.collect_raw_artifacts(root, "next", targets, {"status": "success"})
    manifest = json.loads((root / "profile_manifest.json").read_text())
    assert manifest["status"] == "success"
    assert [case["case"] for case in manifest["cases"]] == ["load", "next"]
    with pytest.raises(FileExistsError):
        capture.initialize_profile_run(root, capture.ProfileSpec())
    assert json.loads((root / "profile_manifest.json").read_text()) == manifest


def test_compression_failure_keeps_source(tmp_path, monkeypatch):
    root = tmp_path / "capture"
    capture.initialize_profile_run(root, capture.ProfileSpec(enabled=True))
    target = capture.ServeInstance.from_endpoint("worker", "http://worker", root=root)
    raw = Path(target.raw_trace_dir)
    raw.mkdir(parents=True)
    (raw / "trace").write_bytes(b"data")
    monkeypatch.setattr(capture.tarfile, "open", Mock(side_effect=OSError("disk full")))
    with pytest.raises(storage.ArtifactError, match="disk full"):
        capture.collect_raw_artifacts(root, "load", (target,), {"status": "success"})
    manifest = json.loads((root / "profile_manifest.json").read_text())
    assert manifest["status"] == "partial"
    assert manifest["cases"][0]["targets"][0]["artifact_error"] == "OSError: disk full"
    assert (raw / "trace").read_bytes() == b"data"


def test_raw_upload_reports_partial_results(raw_job, obs, capsys):
    root, _ = raw_job
    obs.fail_upload = "job/raw/load/worker-1.tar.gz"
    manifest = capture.upload_raw_artifacts(root, "job", "bucket", "https://obs.example", "region")
    assert manifest["status"] == "partial"
    first, second = manifest["cases"][0]["targets"]
    assert first["obs_url"] == "obs://bucket/job/raw/load/worker-0.tar.gz"
    assert second["upload_error"] == "OSError: upload failed"
    assert json.loads(obs.objects["bucket", "job/raw/profile_manifest.json"]) == manifest
    assert obs.uploads[-1] == "job/raw/profile_manifest.json"
    summary = capsys.readouterr().out.split("Profiling downloads", 1)[1]
    assert "https://bucket.obs.example/job/raw/load/worker-0.tar.gz" in summary
    assert "https://bucket.obs.example/job/raw/load/worker-1.tar.gz" not in summary


def test_incomplete_capture_saves_artifacts_and_fails_the_job(raw_job, obs, monkeypatch):
    root, targets = raw_job
    for target in targets:
        (Path(target.raw_trace_dir) / "partial.bin").write_bytes(b"partial trace")
    with pytest.raises(storage.ArtifactError, match="stop_error: HTTP 500"):
        capture.collect_raw_artifacts(
            root,
            "failed",
            targets,
            {
                "status": "partial",
                "reason": "duration_reached",
                "targets": {targets[0].name: {"stop_error": "HTTP 500"}},
            },
        )
    manifest = json.loads((root / "profile_manifest.json").read_text())
    assert manifest["status"] == "partial"
    case = manifest["cases"][-1]
    assert case["status"] == "partial"
    assert all((root / target["archive"]).is_file() for target in case["targets"])
    monkeypatch.setattr(
        sys, "argv", ["capture", "upload", "--root", str(root), "--prefix", "job", "--bucket", "bucket"]
    )
    with pytest.raises(SystemExit, match="incomplete"):
        capture.main()
    assert json.loads(obs.objects["bucket", "job/raw/profile_manifest.json"])["status"] == "partial"


@pytest.mark.parametrize("failure", [None, "analysis", "incomplete", "upload", "download", "missing_shard"])
def test_parse_and_finalize_node_shards(tmp_path, raw_job, obs, monkeypatch, capsys, failure):
    root, _ = raw_job
    capture.upload_raw_artifacts(root, "job", "bucket", "https://obs.example", "region")
    nodes = parse.plan_artifact_shards(tmp_path / "plan", "job", "bucket", "https://obs.example", "region")
    assert nodes == [0, 1]

    def analyse(directory):
        trace = Path(directory)
        if trace.parent.name == "worker-1" and failure == "analysis":
            raise RuntimeError("analysis failed")
        output = trace / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        (output / "trace_view.json").write_text('{"traceEvents":[]}')
        if trace.parent.name != "worker-1" or failure != "incomplete":
            (output / "analyse.done").touch()

    for name in ("torch_npu", "torch_npu.profiler", "torch_npu.profiler.profiler"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    sys.modules["torch_npu.profiler.profiler"].analyse = analyse
    if failure == "upload":
        obs.fail_upload = "job/parsed/load/worker-1/rank_ascend_pt.tar.gz"
    if failure == "download":
        del obs.objects["bucket", "job/raw/load/worker-1.tar.gz"]
    for node in nodes:
        if node == 1 and failure == "missing_shard":
            continue
        shard = parse.parse_artifact_shard(
            tmp_path / f"node-{node}", "job", "bucket", "https://obs.example", "region", node
        )
        expected = "partial" if node == 1 and failure else "success"
        assert shard["status"] == expected
        assert [target["record"]["node_index"] for target in shard["targets"]] == [node]

    arguments = (tmp_path / "final", "job", "bucket", "https://obs.example", "region")
    if failure:
        with pytest.raises(storage.ArtifactError, match="incomplete"):
            parse.finalize_parsed_manifest(*arguments)
    else:
        assert parse.finalize_parsed_manifest(*arguments)["status"] == "success"
    manifest = json.loads(obs.objects["bucket", "job/parsed/profile_manifest.json"])
    assert manifest["status"] == ("partial" if failure else "success")
    first, second = manifest["cases"][0]["targets"]
    assert first["raw_obs_url"].endswith("/raw/load/worker-0.tar.gz")
    assert first["ranks"][0]["obs_url"].endswith("/parsed/load/worker-0/rank_ascend_pt.tar.gz")
    if failure:
        assert second.get("parse_error") or any(
            rank.get("parse_error") or rank.get("upload_error") for rank in second["ranks"]
        )
    assert obs.uploads[-1] == "job/parsed/profile_manifest.json"
    data = obs.objects["bucket", "job/parsed/load/worker-0/rank_ascend_pt.tar.gz"]
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        files = {Path(member.name).name for member in archive.getmembers() if member.isfile()}
    assert files == {"trace_view.json", "analyse.done", "profiler_info.json"}
    summary = capsys.readouterr().out.rsplit("Profiling downloads", 1)[1]
    assert "https://bucket.obs.example/job/raw/load/worker-0.tar.gz" in summary
    assert "https://bucket.obs.example/job/parsed/load/worker-0/rank_ascend_pt.tar.gz" in summary


@pytest.mark.parametrize("upload_fails", [False, True])
def test_rank_parsing_keeps_one_result_waiting_for_publication(rank_job, obs, monkeypatch, upload_fails):
    context, target_dir, traces = rank_job
    upload_started = Event()
    release_upload = Event()
    second_parsed = Event()
    third_started = Event()
    upload_file = obs.upload_file
    first_key = "job/parsed/load/worker/rank-0_ascend_pt.tar.gz"
    if upload_fails:
        obs.fail_upload = first_key

    def slow_upload(source, bucket, key, **kwargs):
        if key == first_key:
            upload_started.set()
            assert release_upload.wait(5)
        upload_file(source, bucket, key, **kwargs)

    def analyse(directory):
        index = traces.index(Path(directory))
        if index == 2:
            third_started.set()
        if index >= 2:
            assert not traces[index - 2].exists()
        context.analyse(directory)
        if index == 1:
            second_parsed.set()

    monkeypatch.setattr(obs, "upload_file", slow_upload)
    target_record = {"name": "worker"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            parse._parse_target_ranks,
            replace(context, analyse=analyse),
            case_name="load",
            raw_archive="load/worker.tar.gz",
            target_record=target_record,
            target_dir=target_dir,
        )
        try:
            assert upload_started.wait(5)
            assert second_parsed.wait(5)
            assert not third_started.wait(0.1)
            assert len(list(target_dir.rglob("ASCEND_PROFILER_OUTPUT"))) == 2
        finally:
            release_upload.set()
        future.result(timeout=5)

    failures = parse._target_failures(target_record)
    assert failures == (["worker/rank-0_ascend_pt: upload_error: OSError: upload failed"] if upload_fails else [])
    assert len(obs.uploads) == (3 if upload_fails else 4)
    assert all("obs_url" in rank for rank in target_record["ranks"][int(upload_fails) :])
    assert not any(trace.exists() for trace in traces)
    assert not list(context.parsed_root.rglob("*.tar.gz"))


@pytest.mark.parametrize("failure", ["analysis", "incomplete", "compression", "upload"])
@pytest.mark.parametrize("failed_index", [0, 3])
def test_rank_failures_release_workspace_and_preserve_other_results(rank_job, obs, monkeypatch, failure, failed_index):
    context, target_dir, traces = rank_job
    failed_trace = traces[failed_index]

    def analyse(directory):
        trace = Path(directory)
        if traces.index(trace) > failed_index + 1:
            assert not failed_trace.exists()
        context.analyse(directory)
        if trace == failed_trace:
            if failure == "analysis":
                raise RuntimeError("analysis failed after writing output")
            if failure == "incomplete":
                (trace / "ASCEND_PROFILER_OUTPUT" / "analyse.done").unlink()

    archive_name = f"{failed_trace.name}.tar.gz"
    if failure == "upload":
        obs.fail_upload = f"job/parsed/load/worker/{archive_name}"
    if failure == "compression":
        tar_open = tarfile.open

        def fail_compression(path, *args, **kwargs):
            if Path(path).name == archive_name:
                Path(path).write_bytes(b"incomplete archive")
                raise OSError("compression failed")
            return tar_open(path, *args, **kwargs)

        monkeypatch.setattr(parse.tarfile, "open", fail_compression)

    target_record = {"name": "worker"}
    parse._parse_target_ranks(
        replace(context, analyse=analyse),
        case_name="load",
        raw_archive="load/worker.tar.gz",
        target_record=target_record,
        target_dir=target_dir,
    )
    error_field = "parse_error" if failure in ("analysis", "incomplete") else "upload_error"
    assert target_record["ranks"][failed_index][error_field]
    assert len(parse._target_failures(target_record)) == 1
    assert all("obs_url" in rank for index, rank in enumerate(target_record["ranks"]) if index != failed_index)
    assert len(obs.uploads) == 3
    assert not any(trace.exists() for trace in traces)
    assert not list(context.parsed_root.rglob("*.tar.gz"))


def test_empty_job_skips_parsing_but_missing_manifest_fails(tmp_path, obs):
    root = tmp_path / "capture"
    capture.initialize_profile_run(root, capture.ProfileSpec(enabled=True))
    manifest = capture.upload_raw_artifacts(root, "job", "bucket", "https://obs.example", "region")
    assert manifest["status"] == "skipped"
    arguments = (tmp_path / "parse", "job", "bucket", "https://obs.example", "region")
    assert parse.plan_artifact_shards(*arguments) == []
    assert parse.parse_artifact_shard(*arguments, node_index=0)["status"] == "skipped"
    assert parse.finalize_parsed_manifest(*arguments)["status"] == "skipped"
    assert obs.uploads == ["job/raw/profile_manifest.json"]
    with pytest.raises(storage.ArtifactError, match="manifest is missing"):
        capture.upload_raw_artifacts(tmp_path / "absent", "job", "bucket", "https://obs.example", "region")


def test_json_upload_propagates_sdk_failure_and_download_url(tmp_path):
    client = MemoryObs()
    source = tmp_path / "summary.json"
    source.write_text("{}")
    client.fail_upload = "summary.json"
    with pytest.raises(OSError, match="upload failed"):
        storage.upload_json(client, source, "bucket", "summary.json")
    assert storage.obs_http_url("obs://bucket/job/a b#1.tar.gz", "https://obs.example") == (
        "https://bucket.obs.example/job/a%20b%231.tar.gz"
    )
