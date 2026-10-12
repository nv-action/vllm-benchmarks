import json
import runpy
import shlex
import sys
import tarfile
import types
from pathlib import Path
from unittest.mock import Mock

import pytest
import yaml
from boto3.exceptions import S3UploadFailedError
from botocore.exceptions import ClientError, EndpointConnectionError

from tools.profiling import capture, parse, storage


def test_obs_defaults_and_virtual_hosted_client(monkeypatch):
    client_factory = Mock()
    monkeypatch.setattr("boto3.client", client_factory)
    storage.create_obs_client(storage.DEFAULT_ENDPOINT, storage.DEFAULT_REGION)
    args, kwargs = client_factory.call_args
    assert args == ("s3",)
    assert kwargs["endpoint_url"] == "https://obs.cn-southwest-2.myhuaweicloud.com"
    assert kwargs["region_name"] == "cn-southwest-2"
    assert kwargs["config"].s3["addressing_style"] == "virtual"
    assert storage.DEFAULT_BUCKET == "ascend-profile-artifacts"
    assert capture.DEFAULT_BUCKET == parse.DEFAULT_BUCKET == storage.DEFAULT_BUCKET
    assert (
        storage.obs_http_url(f"obs://{storage.DEFAULT_BUCKET}/job/raw/server trace.tar.gz", storage.DEFAULT_ENDPOINT)
        == "https://ascend-profile-artifacts.obs.cn-southwest-2.myhuaweicloud.com/job/raw/server%20trace.tar.gz"
    )


@pytest.mark.parametrize(
    "workflow_name", ["nightly_single_node", "nightly_single_node_560t", "nightly_multi_node", "profile_parse"]
)
def test_workflows_isolate_profile_credentials(workflow_name):
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.load((root / f".github/workflows/_e2e_{workflow_name}.yaml").read_text(), Loader=yaml.BaseLoader)
    parser_workflow = workflow_name == "profile_parse"
    secrets = workflow["on"]["workflow_call"]["secrets"]
    for name in ("HW_OBS_PROFILE_AK", "HW_OBS_PROFILE_SK"):
        assert secrets[name]["required"] == str(parser_workflow).lower()
    profile_steps = []
    benchmark_steps = []
    for job in workflow["jobs"].values():
        for step in job.get("steps", []):
            if "tools.profiling.storage status" in step.get("run", "") or "tools.profiling.parse " in step.get(
                "run", ""
            ):
                profile_steps.append(step)
                assert step["env"]["AWS_ACCESS_KEY_ID"] == "${{ secrets.HW_OBS_PROFILE_AK }}"
                assert step["env"]["AWS_SECRET_ACCESS_KEY"] == "${{ secrets.HW_OBS_PROFILE_SK }}"
            if step.get("uses", "").startswith("ascend-gha-runners/artifact/upload@"):
                benchmark_steps.append(step)
                assert step["with"]["access_key"] == "${{ secrets.OBS_ACCESS_KEY_ID }}"
                assert step["with"]["secret_key"] == "${{ secrets.OBS_SECRET_ACCESS_KEY }}"
    assert len(profile_steps) == (3 if parser_workflow else 1)
    assert bool(benchmark_steps) == (not parser_workflow)


class MemoryObs:
    def __init__(self, objects: dict[str, bytes] | None = None):
        self.objects = objects or {}

    def upload_file(self, source, _bucket, key, Callback=None, **_kwargs):
        data = Path(source).read_bytes()
        self.objects[key] = data
        if Callback:
            Callback(len(data))

    def download_file(self, bucket, key, destination, Callback=None, **_kwargs):
        if key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": key}}, "GetObject")
        data = self.objects[key]
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        if Callback:
            Callback(len(data))

    def head_object(self, _Bucket=None, Key=None, **_kwargs):
        return {"ContentLength": len(self.objects[Key])}


def test_storage_upload_helpers_verify_remote_size(tmp_path: Path):
    client = MemoryObs()
    archive = tmp_path / "case" / "server.tar.gz"
    archive.parent.mkdir()
    archive.write_bytes(b"archive")
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}")
    record = {"archive": "case/server.tar.gz"}

    storage.upload_archive(client, object(), tmp_path, "job/raw", "bucket", record, "raw")
    storage.upload_json(client, manifest, "bucket", "job/raw/profile_manifest.json")

    assert storage.GZIP_COMPRESSION_LEVEL == 1
    assert record["obs_url"] == "obs://bucket/job/raw/case/server.tar.gz"
    assert client.objects["job/raw/case/server.tar.gz"] == b"archive"
    assert client.objects["job/raw/profile_manifest.json"] == b"{}"

    client.head_object = lambda **_kwargs: {"ContentLength": 0}
    with pytest.raises(RuntimeError, match="OBS size mismatch"):
        storage.upload_json(client, manifest, "bucket", "job/raw/profile_manifest.json")


def test_plan_artifact_shards_uses_manifest_node_indexes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    manifest = {
        "cases": [
            {"targets": [{"node_index": 2}, {"node_index": 0}]},
            {"targets": [{"node_index": 2}]},
        ]
    }
    client = MemoryObs({"job/raw/profile_manifest.json": json.dumps(manifest).encode()})
    monkeypatch.setattr(parse, "create_obs_client", lambda *_: client)
    monkeypatch.setattr(parse, "create_transfer_config", object)

    assert parse.plan_artifact_shards(tmp_path, "job", "bucket", "endpoint", "region") == [0, 2]


@pytest.mark.parametrize("failure", [None, "parse", "upload", "manifest"])
def test_parse_artifacts_end_to_end(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, failure):
    source = tmp_path / "source" / "server"
    for rank in ("rank0_ascend_pt", "rank1_ascend_pt"):
        trace = source / rank
        trace.mkdir(parents=True)
        (trace / "profiler_info.json").write_text("{}")
    archive = tmp_path / "server.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(source, arcname="server")

    raw_manifest = {
        "status": "success",
        "cases": [
            {
                "case": "case-a",
                "status": "success",
                "targets": [
                    {
                        "name": "server",
                        "node_index": 0,
                        "archive": "case-a/server.tar.gz",
                        "obs_url": "obs://bucket/job/raw/case-a/server.tar.gz",
                        "size_bytes": archive.stat().st_size,
                        "output": "raw",
                    }
                ],
            }
        ],
    }
    client = MemoryObs(
        {
            "job/raw/profile_manifest.json": json.dumps(raw_manifest).encode(),
            "job/raw/case-a/server.tar.gz": archive.read_bytes(),
        }
    )
    monkeypatch.setattr(parse, "create_obs_client", lambda *_: client)
    monkeypatch.setattr(parse, "create_transfer_config", object)
    # Exercise both pools with more than one worker, as future CI may do.
    monkeypatch.setattr(parse, "RANK_PARSE_CONCURRENCY", 2)
    monkeypatch.setattr(parse, "PUBLISH_CONCURRENCY", 2)
    original_upload = client.upload_file

    def upload(source, bucket, key, **kwargs):
        if (failure == "upload" and key.endswith("rank0_ascend_pt.tar.gz")) or (
            failure == "manifest" and key == "job/parsed/profile_manifest.json"
        ):
            raise ClientError({"Error": {"Code": "AccessDenied", "Message": "private-service:8000"}}, "PutObject")
        original_upload(source, bucket, key, **kwargs)

    monkeypatch.setattr(client, "upload_file", upload)

    def analyse(trace_dir: str, max_process_number: int) -> None:
        assert max_process_number == 4
        if failure == "parse" and Path(trace_dir).name == "rank0_ascend_pt":
            return  # Simulate torch-npu returning without complete output.
        output = Path(trace_dir) / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        (output / "analyse.done").touch()
        (output / "trace_view.json").write_text("{}")

    profiler_module = types.ModuleType("torch_npu.profiler.profiler")
    profiler_module.analyse = analyse
    monkeypatch.setitem(sys.modules, "torch_npu", types.ModuleType("torch_npu"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler", types.ModuleType("torch_npu.profiler"))
    monkeypatch.setitem(sys.modules, "torch_npu.profiler.profiler", profiler_module)

    args = (tmp_path / "parse", "job", "bucket", "https://obs.example.com", "region")
    shard = parse.parse_artifact_shard(*args, node_index=0, max_process_number=4)
    assert len(shard["failures"]) == (1 if failure in ("parse", "upload") else 0)
    if failure:
        expected_error = ClientError if failure == "manifest" else storage.ArtifactError
        with pytest.raises(expected_error):
            parse.finalize_parsed_manifest(*args)
        result = json.loads((args[0] / "parsed/profile_manifest.json").read_text())
    else:
        result = parse.finalize_parsed_manifest(*args)

    target = result["cases"][0]["targets"][0]
    assert result["status"] == ("partial" if failure in ("parse", "upload") else "success")
    assert "endpoint" not in target
    assert target["raw_obs_url"] == "obs://bucket/job/raw/case-a/server.tar.gz"
    assert target["ranks"][1]["output"] == "parsed"
    assert target["ranks"][1]["obs_url"].startswith("obs://bucket/job/parsed/")
    assert ("job/parsed/profile_manifest.json" in client.objects) == (failure != "manifest")
    assert "job/raw/case-a/server.tar.gz" in client.objects
    assert "private-service" not in json.dumps(shard) + json.dumps(result)
    output = capsys.readouterr().out
    assert "private-service" not in output
    summary = output.split("Profiling downloads", 1)[1]
    assert "https://bucket.obs.example.com/job/raw/case-a/server.tar.gz" in summary
    assert "https://bucket.obs.example.com/job/parsed/case-a/server/rank1_ascend_pt.tar.gz" in summary
    assert "obsutil cp obs://bucket/job/parsed/ ./profile-download/parsed/ -r -j=4" in summary
    if failure in ("parse", "upload"):
        assert "rank0_ascend_pt.tar.gz" not in summary
        _, failures = parse._merge_parsed_shards(raw_manifest, [shard])
        assert len(failures) == 1
    if failure == "manifest":
        assert "https://bucket.obs.example.com/job/parsed/profile_manifest.json" not in summary


def test_parse_artifacts_rejects_missing_raw_manifest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    client = MemoryObs()
    monkeypatch.setattr(parse, "create_obs_client", lambda *_: client)
    monkeypatch.setattr(parse, "create_transfer_config", object)

    with pytest.raises(ClientError):
        parse.parse_artifact_shard(tmp_path, "job", "bucket", "endpoint", "region", node_index=0)


def test_no_selected_cases_skip_the_entire_parse_pipeline(tmp_path, monkeypatch):
    raw = {"status": "skipped", "cases": []}
    client = MemoryObs({"job/raw/profile_manifest.json": json.dumps(raw).encode()})
    monkeypatch.setattr(parse, "create_obs_client", lambda *_: client)
    monkeypatch.setattr(parse, "create_transfer_config", object)
    args = (tmp_path, "job", "bucket", "https://obs.example.com", "region")
    assert parse.plan_artifact_shards(*args) == []
    assert parse.parse_artifact_shard(*args, node_index=0)["status"] == "skipped"
    assert parse.finalize_parsed_manifest(*args) == raw
    assert list(client.objects) == ["job/raw/profile_manifest.json"]


@pytest.mark.parametrize("missing", [None, "analyse.done", "trace_view.json", "empty_trace"])
def test_analysis_checks_completion_and_artifact_presence_only(tmp_path, missing):
    def analyse(trace_dir, **_):
        output = Path(trace_dir) / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        if missing != "analyse.done":
            (output / "analyse.done").touch()
        if missing != "trace_view.json":
            (output / "trace_view.json").write_text("" if missing == "empty_trace" else "not validated as JSON")

    context = types.SimpleNamespace(analyse=analyse, node_index=0, max_process_number=4)
    kwargs = dict(raw_archive="server.tar.gz", trace_index=1, trace_count=1, trace_dir=tmp_path)
    if missing:
        with pytest.raises(storage.ArtifactError, match="Incomplete parsed trace"):
            parse._analyse_rank_trace(context, **kwargs)
    else:
        assert parse._analyse_rank_trace(context, **kwargs).name == "ASCEND_PROFILER_OUTPUT"


@pytest.mark.parametrize("raw_status", ["success", "partial"])
def test_finalize_preserves_raw_failures_and_records_missing_shards(tmp_path, monkeypatch, capsys, raw_status):
    raw = {
        "status": raw_status,
        "cases": [
            {
                "case": "case-a",
                "status": raw_status,
                "targets": [
                    {
                        "name": "server",
                        "node_index": 1,
                        "obs_url": "obs://bucket/job/raw/server.tar.gz",
                    }
                ],
            }
        ],
    }
    client = MemoryObs({"job/raw/profile_manifest.json": json.dumps(raw).encode()})
    monkeypatch.setattr(parse, "create_obs_client", lambda *_: client)
    monkeypatch.setattr(parse, "create_transfer_config", object)

    with pytest.raises(storage.ArtifactError, match="1 parse/upload failures, 1 missing shards"):
        parse.finalize_parsed_manifest(tmp_path, "job", "bucket", "https://obs.example.com", "region")

    result = json.loads(client.objects["job/parsed/profile_manifest.json"])
    target = result["cases"][0]["targets"][0]
    assert result["status"] == result["cases"][0]["status"] == "partial"
    assert target["parse_error"] == "Parsed shard result is missing"
    assert "endpoint" not in target
    assert "https://bucket.obs.example.com/job/raw/server.tar.gz" in capsys.readouterr().out


def test_merge_preserves_raw_partial_status():
    raw = {"status": "partial", "cases": [{"case": "case-a", "status": "partial", "targets": [{"name": "server"}]}]}
    shard = {"targets": [{"case_index": 0, "target_index": 0, "record": {"name": "server", "ranks": []}}]}

    result, failures = parse._merge_parsed_shards(raw, [shard])

    assert failures == []
    assert result["status"] == result["cases"][0]["status"] == "partial"


def test_parse_cli_requires_node_and_reports_partial_after_publication(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["parse", "parse", "--root", "/tmp/profile", "--prefix", "job"])
    with pytest.raises(SystemExit) as error:
        parse.main()
    assert error.value.code == 2

    monkeypatch.setattr(sys, "argv", [*sys.argv, "--node-index", "0"])
    monkeypatch.setattr(parse, "parse_artifact_shard", lambda *_: {"status": "partial", "failures": ["rank failed"]})
    with pytest.raises(storage.ArtifactError, match="1 parse/upload failures"):
        parse.main()


def test_download_summary_quotes_keys_and_shell_arguments(capsys):
    prefix = "job's results"
    manifest = {
        "status": "success",
        "cases": [
            {"case": "case-a", "targets": [{"name": "server", "obs_url": f"obs://bucket/{prefix}/raw/a #中.tar.gz"}]}
        ],
    }

    storage.print_download_summary(manifest, "bucket", prefix, "https://obs.example.com", "raw", False)

    output = capsys.readouterr().out
    assert "https://bucket.obs.example.com/job%27s%20results/raw/a%20%23%E4%B8%AD.tar.gz" in output
    command = next(line for line in output.splitlines() if line.startswith("obsutil cp"))
    assert shlex.split(command) == [
        "obsutil",
        "cp",
        f"obs://bucket/{prefix}/raw/",
        "./profile-download/raw/",
        "-r",
        "-j=4",
    ]


@pytest.mark.parametrize("module, command", [(capture, "upload"), (parse, "plan")])
@pytest.mark.parametrize(
    "error",
    [
        EndpointConnectionError(endpoint_url="http://private-service:8000"),
        S3UploadFailedError("private-service:8000"),
        storage.ArtifactError("OBS size mismatch"),
    ],
)
def test_cli_errors_exit_without_infrastructure_details(tmp_path, monkeypatch, module, command, error):
    (tmp_path / "profile_manifest.json").write_text('{"status": "success", "cases": []}')

    def fail_client(*_):
        raise error

    monkeypatch.setattr(storage, "create_obs_client", fail_client)
    monkeypatch.setattr(sys, "argv", ["profile", command, "--root", str(tmp_path), "--prefix", "job"])

    with pytest.raises(SystemExit) as result:
        runpy.run_path(module.__file__, run_name="__main__")

    message = str(result.value)
    assert "private-service" not in message
    assert (
        "OBS size mismatch" in message if isinstance(error, storage.ArtifactError) else type(error).__name__ in message
    )
