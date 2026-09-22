"""Capture archives, offline parsing, and the downloadable artifact bundle."""

import json
import os
import shutil
import subprocess
import sys
import tarfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest

from tools.profiling import artifacts, capture, parse, storage


@pytest.fixture
def object_store(tmp_path, monkeypatch):
    objects = tmp_path / "objects"

    def upload(source, bucket, key, **kwargs):
        destination = objects / bucket / key
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)

    def download(bucket, key, destination, **kwargs):
        shutil.copyfile(objects / bucket / key, destination)

    client = Mock(upload_file=Mock(side_effect=upload), download_file=Mock(side_effect=download))
    for module in (capture, parse):
        monkeypatch.setattr(module, "create_obs_client", lambda *_: client)
        monkeypatch.setattr(module, "create_transfer_config", lambda: None)
    return SimpleNamespace(client=client, root=objects)


@pytest.fixture
def analyser(monkeypatch):
    def analyse(directory):
        output = Path(directory) / "ASCEND_PROFILER_OUTPUT"
        output.mkdir()
        (output / "trace_view.json").write_text('{"traceEvents":[]}')
        (output / "analyse.done").touch()

    for name in ("torch_npu", "torch_npu.profiler", "torch_npu.profiler.profiler"):
        monkeypatch.setitem(sys.modules, name, ModuleType(name))
    analyser = Mock(side_effect=analyse)
    sys.modules["torch_npu.profiler.profiler"].analyse = analyser
    return analyser


@pytest.fixture
def capture_run(tmp_path):
    def create(output="parsed", ranks=(0, 1, 10)):
        root = tmp_path / "capture"
        capture.initialize_profile_run(root, capture.ProfileSpec(enabled=True, output=output))
        targets = tuple(
            capture.ServeInstance.from_endpoint(f"dp-{node}", "http://leader:8000", node_index=node, root=root)
            for node in (0, 1)
        )
        capture.ServeManifest(targets).write(root / "serve_manifest.json")
        capture.prepare_capture(root, "load", "model__load", targets)
        for target in targets:
            for rank in ranks:
                trace = Path(target.raw_trace_dir) / f"dp{target.node_index}_pp0_tp{rank}_rank{rank}_ascend_pt"
                trace.mkdir(parents=True)
                (trace / "raw.bin").write_bytes(b"raw profile")
                (trace / "profiler_info.json").write_text('{"device":"npu"}')
        capture.collect_raw_artifacts(
            root, "model__load", targets, {"status": "success", "reason": "duration_reached", "actual_duration": 8}
        )
        return SimpleNamespace(root=root, manifest=root / "profile_manifest.json", targets=targets)

    return create


def upload_raw(run):
    return capture.upload_raw_artifacts(run.root, "job", "bucket", "https://obs.example", "region")


@pytest.fixture
def published_bundle(tmp_path, capture_run, object_store, analyser):
    run = capture_run()
    upload_raw(run)
    bundle, shards = tmp_path / "profile_artifact", tmp_path / "shards"
    artifacts.write_artifact_bundle(run.manifest, bundle)
    shards.mkdir()
    for node in parse.plan_artifact_shards(bundle / "profile_manifest.json"):
        work = tmp_path / f"node-{node}"
        parse.parse_artifact_shard(work, bundle / "profile_manifest.json", node)
        shutil.copyfile(work / f"parsed/shards/node-{node}.json", shards / f"node-{node}.json")
    parse.finalize_parsed_manifest(bundle, shards)
    artifacts.write_artifact_bundle(bundle / "profile_manifest.json", bundle)
    return bundle


def test_raw_archives_contain_trace_data_and_release_capture_directories(capture_run):
    run = capture_run()
    manifest = json.loads(run.manifest.read_text())
    assert manifest["status"] == "success"
    for target, record in zip(run.targets, manifest["benchmarks"][0]["targets"]):
        assert not list(Path(target.raw_trace_dir).iterdir())
        with tarfile.open(run.root / record["archive"]) as archive:
            raw_files = [member for member in archive.getmembers() if member.name.endswith("/raw.bin")]
            assert len(raw_files) == 3
            assert all(archive.extractfile(member).read() == b"raw profile" for member in raw_files)


def test_serving_snapshots_are_isolated_between_benchmarks(tmp_path, capture_run):
    run = capture_run()
    target = capture.ServeInstance.from_endpoint("replacement", "http://other:8000", root=run.root)
    capture.ServeManifest((target,)).write(run.root / "serve_manifest.json")
    capture.prepare_capture(run.root, "load", "other_model__load", (target,))
    before = run.manifest.read_text()
    with pytest.raises(FileExistsError):
        capture.prepare_capture(run.root, "load", "model__load", (target,))
    assert run.manifest.read_text() == before
    bundle = tmp_path / "index"
    artifacts.write_artifact_bundle(run.manifest, bundle, "job")
    first, second = json.loads(before)["benchmarks"]
    assert capture.ServeManifest.read(bundle / first["serve_manifest"]).instances == run.targets
    assert capture.ServeManifest.read(bundle / second["serve_manifest"]).instances == (target,)


def test_compression_error_preserves_raw_data_and_records_failure(capture_run, monkeypatch):
    run = capture_run()
    target = run.targets[0]
    source = Path(target.raw_trace_dir) / "raw.bin"
    source.write_bytes(b"unarchived data")
    capture.prepare_capture(run.root, "next", "model__next", (target,))
    monkeypatch.setattr(capture.tarfile, "open", Mock(side_effect=OSError("disk full")))
    with pytest.raises(storage.ArtifactError, match="disk full"):
        capture.collect_raw_artifacts(run.root, "model__next", (target,), {"status": "success"})
    manifest = json.loads(run.manifest.read_text())
    assert manifest["status"] == "partial"
    assert "disk full" in manifest["benchmarks"][-1]["targets"][0]["artifact_error"]
    assert source.read_bytes() == b"unarchived data"


@pytest.mark.parametrize("output", ["raw", "parsed"])
def test_capture_index_contains_metadata_and_confirmed_download_links(tmp_path, capture_run, object_store, output):
    run = capture_run(output=output)
    manifest = upload_raw(run)
    bundle = tmp_path / "index"
    artifacts.write_artifact_bundle(run.manifest, bundle)
    assert (bundle / "profile_manifest.json").is_file()
    assert (bundle / manifest["benchmarks"][0]["serve_manifest"]).is_file()
    assert (bundle / "download.sh").is_file()
    assert not list(bundle.rglob("*.tar.gz")) and not list(bundle.rglob("*.bin"))
    assert (bundle / "parsed").exists() is (output == "parsed")
    index = (bundle / "raw/downloads.md").read_text()
    assert "https://bucket.obs.example/job/raw/model__load/dp-0.tar.gz" in index
    assert "https://bucket.obs.example/job/raw/model__load/dp-1.tar.gz" in index
    assert all(path.suffix == ".gz" for path in object_store.root.rglob("*") if path.is_file())


def test_failed_raw_upload_is_fatal_and_keeps_successful_links(tmp_path, capture_run, object_store, monkeypatch):
    run = capture_run()
    upload = object_store.client.upload_file.side_effect

    def upload_with_failure(source, bucket, key, **kwargs):
        if key.endswith("dp-1.tar.gz"):
            raise OSError("upload unavailable")
        upload(source, bucket, key, **kwargs)

    object_store.client.upload_file.side_effect = upload_with_failure
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "upload",
            "--root",
            str(run.root),
            "--prefix",
            "job",
            "--bucket",
            "bucket",
            "--endpoint",
            "https://obs.example",
        ],
    )
    with pytest.raises(SystemExit, match="incomplete"):
        capture.main()
    bundle = tmp_path / "index"
    artifacts.write_artifact_bundle(run.manifest, bundle)
    manifest = json.loads((bundle / "profile_manifest.json").read_text())
    assert manifest["status"] == "partial"
    summary = (bundle / "summary.txt").read_text()
    assert "upload unavailable" in summary
    assert "https://bucket.obs.example/job/raw/model__load/dp-0.tar.gz" in summary
    assert "https://bucket.obs.example/job/raw/model__load/dp-1.tar.gz" not in summary


def test_parsed_bundle_combines_raw_and_rank_outputs(published_bundle, object_store):
    manifest = json.loads((published_bundle / "profile_manifest.json").read_text())
    assert manifest["status"] == "success"
    for target in manifest["benchmarks"][0]["targets"]:
        assert target["obs_url"].endswith(f"/raw/model__load/{target['name']}.tar.gz")
        assert len(target["ranks"]) == 3
        for rank in target["ranks"]:
            archive = object_store.root / "bucket/job/parsed" / rank["archive"]
            with tarfile.open(archive) as stream:
                files = {Path(member.name).name for member in stream.getmembers() if member.isfile()}
            assert files == {"trace_view.json", "analyse.done", "profiler_info.json"}
    uploads = [path for path in object_store.root.rglob("*") if path.is_file()]
    assert len(uploads) == 8 and all(path.name.endswith(".tar.gz") for path in uploads)
    summary = (published_bundle / "summary.txt").read_text()
    assert "QUICK DOWNLOAD" in summary and "RAW" in summary and "PARSED" in summary
    assert summary.index("TP=1 ") < summary.index("TP=10 ")
    assert summary.count("https://") == 8
    assert "obsutil cp" in summary and "-j=4" in summary
    assert "bash ./profile_artifact/download.sh all" in summary


@pytest.mark.parametrize("failure", ["analysis", "incomplete_analysis", "upload", "missing_shard"])
def test_partial_parsing_preserves_raw_and_successful_parsed_results(
    tmp_path, capture_run, object_store, analyser, failure
):
    run = capture_run()
    upload_raw(run)
    bundle, shards = tmp_path / "index", tmp_path / "shards"
    artifacts.write_artifact_bundle(run.manifest, bundle)
    shards.mkdir()
    analyse = analyser.side_effect
    upload = object_store.client.upload_file.side_effect

    def analyse_with_failure(directory):
        if "dp0_pp0_tp1_" in directory and failure in ("analysis", "incomplete_analysis"):
            if failure == "analysis":
                raise RuntimeError("analysis unavailable")
            return  # A successful return without profiler output is incomplete.
        analyse(directory)

    def upload_with_failure(source, bucket, key, **kwargs):
        if failure == "upload" and "/parsed/" in key and "dp0_pp0_tp1_" in key:
            raise OSError("upload unavailable")
        upload(source, bucket, key, **kwargs)

    analyser.side_effect = analyse_with_failure
    object_store.client.upload_file.side_effect = upload_with_failure
    for node in parse.plan_artifact_shards(run.manifest):
        if failure == "missing_shard" and node == 1:
            continue
        work = tmp_path / f"node-{node}"
        parse.parse_artifact_shard(work, run.manifest, node)
        shutil.copyfile(work / f"parsed/shards/node-{node}.json", shards / f"node-{node}.json")
    with pytest.raises(storage.ArtifactError, match="incomplete"):
        parse.finalize_parsed_manifest(bundle, shards)
    artifacts.write_artifact_bundle(bundle / "profile_manifest.json", bundle)
    manifest = json.loads((bundle / "profile_manifest.json").read_text())
    assert manifest["status"] == "partial"
    assert all(target["obs_url"] for target in manifest["benchmarks"][0]["targets"])
    summary = (bundle / "summary.txt").read_text()
    assert "/raw/model__load/dp-0.tar.gz" in summary and "/raw/model__load/dp-1.tar.gz" in summary
    assert "/parsed/model__load/dp-0/dp0_pp0_tp0_rank0_ascend_pt.tar.gz" in summary
    assert "FAILURES" in summary


def test_parser_allows_only_one_completed_rank_to_wait_for_upload(tmp_path, capture_run, object_store, analyser):
    run = capture_run(ranks=(0, 1, 2, 3))
    upload_raw(run)
    uploading, release_upload, second_parsed, third_started = (Event() for _ in range(4))
    upload, analyse = object_store.client.upload_file.side_effect, analyser.side_effect
    work = tmp_path / "parser"

    def slow_upload(source, bucket, key, **kwargs):
        if "/parsed/" in key and "tp0_rank0" in key:
            uploading.set()
            assert release_upload.wait(5)
        upload(source, bucket, key, **kwargs)

    def observed_analysis(directory):
        if "tp2_rank2" in directory:
            third_started.set()
        analyse(directory)
        if "tp1_rank1" in directory:
            second_parsed.set()

    object_store.client.upload_file.side_effect = slow_upload
    analyser.side_effect = observed_analysis
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(parse.parse_artifact_shard, work, run.manifest, 0)
        try:
            assert uploading.wait(5) and second_parsed.wait(5)
            assert not third_started.wait(0.1)
            assert len(list(work.rglob("ASCEND_PROFILER_OUTPUT"))) == 2
        finally:
            release_upload.set()
        assert future.result(timeout=5)["status"] == "success"
    assert not list(work.rglob("ASCEND_PROFILER_OUTPUT"))
    assert not list(work.rglob("*.tar.gz"))


def test_metadata_cli_runs_without_optional_packages(tmp_path, capture_run, object_store):
    run = capture_run()
    upload_raw(run)
    bundle, plan = tmp_path / "index", tmp_path / "plan.json"
    artifacts.write_artifact_bundle(run.manifest, bundle)
    command = [sys.executable, "-S", "-m", "tools.profiling.parse"]
    subprocess.run(command + ["plan", "--manifest", str(run.manifest), "--plan-output", str(plan)], check=True)
    assert json.loads(plan.read_text()) == {"node_index": [0, 1]}
    result = subprocess.run(
        command + ["finalize", "--root", str(bundle), "--shard-root", str(tmp_path / "absent")],
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0 and "incomplete" in result.stderr
    command = [sys.executable, "-S", "-m", "tools.profiling.artifacts"]
    subprocess.run(
        command + ["package", "--manifest", str(bundle / "profile_manifest.json"), "--output", str(bundle)],
        check=True,
    )
    summary = subprocess.run(
        command + ["summary", "--root", str(bundle)], check=True, capture_output=True, text=True
    ).stdout
    assert "partial" in summary and "::group::RAW download links" in summary
    assert "https://bucket.obs.example/job/raw/model__load/dp-0.tar.gz" in summary


def test_unselected_run_has_no_parse_jobs(tmp_path, object_store):
    root = tmp_path / "empty"
    capture.initialize_profile_run(root, capture.ProfileSpec(enabled=True))
    capture.upload_raw_artifacts(root, "job", "bucket", "https://obs.example", "region")
    assert parse.plan_artifact_shards(root / "profile_manifest.json") == []
    assert parse.finalize_parsed_manifest(root, tmp_path / "shards")["status"] == "skipped"
    object_store.client.upload_file.assert_not_called()


@pytest.fixture
def curl_command(tmp_path, monkeypatch):
    command = tmp_path / "curl"
    command.write_text(
        "#!/bin/bash\n"
        "while (( $# > 0 )); do\n"
        '  case "$1" in --output) output="$2"; shift ;; esac\n'
        '  url="$1"; shift\n'
        "done\n"
        'printf "%s" "$url" > "$output"\n'
        'exit "$CURL_TEST_EXIT_CODE"\n'
    )
    command.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("CURL_TEST_EXIT_CODE", "0")


@pytest.mark.parametrize("mode,raw_count,parsed_count", [("raw", 2, 0), ("parsed", 0, 6), ("all", 2, 6)])
def test_download_script_selects_outputs_and_preserves_paths(
    published_bundle, curl_command, tmp_path, mode, raw_count, parsed_count
):
    destination = tmp_path / "download folder"
    subprocess.run(["bash", str(published_bundle / "download.sh"), mode, str(destination)], check=True)
    assert len(list((destination / "raw").rglob("*.tar.gz"))) == raw_count
    assert len(list((destination / "parsed").rglob("*.tar.gz"))) == parsed_count
    for path in destination.rglob("*.tar.gz"):
        relative = path.relative_to(destination)
        assert path.read_text() == f"https://bucket.obs.example/job/{relative}"
    assert not list(destination.rglob("*.part"))


def test_download_failure_does_not_publish_a_partial_file(published_bundle, curl_command, tmp_path, monkeypatch):
    monkeypatch.setenv("CURL_TEST_EXIT_CODE", "22")
    destination = tmp_path / "downloads"
    result = subprocess.run(["bash", str(published_bundle / "download.sh"), "all", str(destination)])
    assert result.returncode == 22
    assert not list(destination.rglob("*.tar.gz"))
    assert len(list(destination.rglob("*.part"))) == 1


def test_artifact_names_distinguish_runs_attempts_scenes_and_jobs():
    prefixes = [
        f"nightly-profiling/org/repo/{run}/{attempt}/{scene}/{job}"
        for run in (1, 2)
        for attempt in (1, 2)
        for scene in ("single-node", "multi-node")
        for job in ("main-model-a", "main-model-b", "release-model-a")
    ]
    assert len({artifacts.artifact_key(prefix) for prefix in prefixes}) == len(prefixes)
