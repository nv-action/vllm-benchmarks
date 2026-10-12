import json
import sys
import threading
from pathlib import Path

import jinja2
import pytest
import yaml

from tools.profiling import capture
from tools.profiling.capture import (
    ProfileController,
    ProfileSpec,
    ServeInstance,
    ServeManifest,
    collect_raw_artifacts,
    configure_profiler_command,
    record_first_request,
    upload_raw_artifacts,
)

PROFILE_ENV_NAMES = (
    "ASCEND_PROFILE_ENABLED",
    "ASCEND_PROFILE_START_AFTER",
    "ASCEND_PROFILE_DURATION",
    "ASCEND_PROFILE_WITH_STACK",
    "ASCEND_PROFILE_SCOPE",
    "ASCEND_PROFILE_OUTPUT",
    "ASCEND_PROFILE_CASES",
)


def test_profile_spec_reads_environment_and_filters_cases(monkeypatch: pytest.MonkeyPatch):
    for name in PROFILE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    assert ProfileSpec.from_env() == ProfileSpec()
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "FaLsE")
    assert ProfileSpec.from_env() == ProfileSpec()

    values = {
        "ASCEND_PROFILE_ENABLED": "True",
        "ASCEND_PROFILE_START_AFTER": "0",
        "ASCEND_PROFILE_DURATION": "12",
        "ASCEND_PROFILE_WITH_STACK": "TRUE",
        "ASCEND_PROFILE_SCOPE": "all",
        "ASCEND_PROFILE_OUTPUT": "raw",
        "ASCEND_PROFILE_CASES": "case-a, case-b,",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)

    spec = ProfileSpec.from_env()

    assert spec == ProfileSpec(True, 0, 12, True, "all", "raw", ("case-a", "case-b"))
    assert spec.includes_case("case-a", "performance")
    assert not spec.includes_case("case-c", "performance")
    assert not spec.includes_case("case-a", "accuracy")


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("ASCEND_PROFILE_ENABLED", "yes"),
        ("ASCEND_PROFILE_SCOPE", "first"),
        ("ASCEND_PROFILE_DURATION", "0"),
    ],
)
def test_profile_spec_rejects_invalid_values(monkeypatch: pytest.MonkeyPatch, name: str, value: str):
    monkeypatch.setenv("ASCEND_PROFILE_ENABLED", "true")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError):
        ProfileSpec.from_env()


def test_serve_manifest_round_trip_and_representative_selection(tmp_path: Path):
    instances = (
        ServeInstance.from_endpoint("prefill/1", "http://prefill-1", "prefill", 1, root=tmp_path),
        ServeInstance.from_endpoint("prefill-0", "http://prefill-0", "prefill", 0, root=tmp_path),
        ServeInstance.from_endpoint("decode-0", "http://decode-0", "decode", 0, root=tmp_path),
    )
    path = tmp_path / "serve_manifest.json"
    ServeManifest(instances).write(path)

    manifest = ServeManifest.read(path)

    assert manifest == ServeManifest(instances)
    assert [target.name for target in manifest.select_targets("representative")] == ["prefill-0", "decode-0"]
    assert manifest.select_targets("all") == instances
    assert Path(instances[0].raw_trace_dir).name == "prefill_1"


def test_configure_profiler_command_preserves_unowned_options(tmp_path: Path):
    instance = ServeInstance.from_endpoint("server", "http://server", root=tmp_path)
    command = ["vllm", "serve", "model", "--profiler-config", '{"custom": 7, "max_iterations": 3}']

    updated = configure_profiler_command(command, instance, ProfileSpec(with_stack=True))
    profiler_config = json.loads(updated[updated.index("--profiler-config") + 1])

    assert profiler_config == {
        "custom": 7,
        "max_iterations": 0,
        "profiler": "torch",
        "torch_profiler_dir": instance.raw_trace_dir,
        "torch_profiler_with_stack": True,
        "ignore_frontend": True,
    }


def test_profile_controller_profiles_each_endpoint_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    marker = tmp_path / "first_request"
    record_first_request(marker)
    monkeypatch.setattr(capture, "POLL_INTERVAL_SECONDS", 0.001)
    stopped = threading.Event()

    class Client:
        def __init__(self):
            self.started: list[str] = []
            self.stopped: list[str] = []

        def start_profile(self, endpoint: str) -> None:
            self.started.append(endpoint)

        def stop_profile(self, endpoint: str) -> None:
            self.stopped.append(endpoint)
            stopped.set()

    client = Client()
    targets = (
        ServeInstance("rank-0", "http://server", str(tmp_path / "rank-0")),
        ServeInstance("rank-1", "http://server", str(tmp_path / "rank-1"), dp_rank=1),
    )
    controller = ProfileController(ProfileSpec(True, 0, 0.01), targets, marker, client)

    controller.start()
    assert stopped.wait(1)
    result = controller.finish()

    assert result["status"] == "success"
    assert result["reason"] == "duration_reached"
    assert client.started == ["http://server"]
    assert client.stopped == ["http://server"]


def test_collect_raw_artifacts_records_partial_results(tmp_path: Path):
    tmp_path = tmp_path / "run"
    capture.initialize_profile_run(tmp_path, ProfileSpec(enabled=True))
    good_dir = tmp_path / "raw" / "good"
    good_dir.mkdir(parents=True)
    (good_dir / "trace.bin").write_bytes(b"trace")
    targets = (
        ServeInstance("good", "http://good", str(good_dir)),
        ServeInstance("missing", "http://missing", str(tmp_path / "raw" / "missing")),
    )

    collect_raw_artifacts(
        tmp_path,
        "case/name",
        targets,
        {"status": "success", "reason": "duration_reached", "actual_duration": 8, "targets": {}},
    )

    manifest = json.loads((tmp_path / "profile_manifest.json").read_text())
    good, missing = manifest["cases"][0]["targets"]
    assert capture.RAW_COMPRESSION_CONCURRENCY == 4
    assert manifest["status"] == "partial"
    assert good["archive"] == "case_name/good.tar.gz"
    assert (tmp_path / good["archive"]).is_file()
    assert not any(good_dir.iterdir())
    assert "artifact_error" in missing
    assert all("endpoint" not in target for target in (good, missing))


@pytest.mark.parametrize("manifest_fails", [False, True])
def test_upload_raw_artifacts_publishes_manifest_last(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys, manifest_fails
):
    manifest_path = tmp_path / "profile_manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "status": "success",
                "cases": [
                    {
                        "case": "case-a",
                        "status": "success",
                        "targets": [
                            {"name": "good", "archive": "case/good.tar.gz"},
                            {"name": "bad", "archive": "case/bad.tar.gz", "obs_url": "obs://bucket/old/bad.tar.gz"},
                        ],
                    }
                ],
            }
        )
    )
    events: list[str] = []

    def upload_archive(_client, _config, _root, prefix, bucket, record, _stage):
        events.append(record["name"])
        if record["name"] == "bad":
            raise RuntimeError("upload failed: http://private-service:8000")
        record["obs_url"] = f"obs://{bucket}/{prefix}/{record['archive']}"

    monkeypatch.setattr(capture, "create_obs_client", lambda *_: object())
    monkeypatch.setattr(capture, "create_transfer_config", object)
    monkeypatch.setattr(capture, "upload_archive", upload_archive)

    def upload_manifest(*_):
        events.append("manifest")
        if manifest_fails:
            raise RuntimeError("manifest upload failed")

    monkeypatch.setattr(capture, "upload_json", upload_manifest)

    if manifest_fails:
        with pytest.raises(RuntimeError, match="manifest upload failed"):
            upload_raw_artifacts(tmp_path, "/job/", "bucket", "https://obs.example.com", "region")
        manifest = json.loads(manifest_path.read_text())
    else:
        manifest = upload_raw_artifacts(tmp_path, "/job/", "bucket", "https://obs.example.com", "region")

    good, bad = manifest["cases"][0]["targets"]
    assert manifest["status"] == "partial"
    assert good["obs_url"] == "obs://bucket/job/raw/case/good.tar.gz"
    assert bad["upload_error"] == "RuntimeError"
    assert "obs_url" not in bad
    assert "private-service" not in json.dumps(manifest)
    assert events[-1] == "manifest"
    output = capsys.readouterr().out
    assert "private-service" not in output
    summary = output.split("Profiling downloads", 1)[1]
    assert "https://bucket.obs.example.com/job/raw/case/good.tar.gz" in summary
    assert "bad.tar.gz" not in summary
    assert ("https://bucket.obs.example.com/job/raw/profile_manifest.json" in summary) == (not manifest_fails)


def test_profile_control_errors_do_not_publish_endpoint(tmp_path):
    target = ServeInstance("server", "http://private-service:8000", str(tmp_path))
    controller = ProfileController(ProfileSpec(), (target,), tmp_path / "marker")

    def fail(endpoint):
        raise ConnectionError(f"Cannot connect to {endpoint}")

    assert controller._call_profile_endpoints(fail, (target,)) == {"server": "ConnectionError"}


def test_job_retains_cases_across_service_restarts(tmp_path, monkeypatch):
    root = tmp_path / "run"
    capture.initialize_profile_run(root, ProfileSpec(enabled=True))
    target = ServeInstance.from_endpoint("server", "http://server", root=root)
    for case in ("config-a__performance", "config-b__performance"):
        ServeManifest((target,)).write(root / "serve_manifest.json")
        trace_dir = Path(target.raw_trace_dir)
        trace_dir.mkdir(parents=True, exist_ok=True)
        (trace_dir / "trace.bin").write_bytes(case.encode())
        collect_raw_artifacts(root, case, (target,), {"status": "success", "targets": {}})
    with pytest.raises(FileExistsError):
        capture.initialize_profile_run(root, ProfileSpec(enabled=True))

    uploaded = []

    def upload(_client, _config, _root, prefix, bucket, record, _stage):
        uploaded.append(record["archive"])
        record["obs_url"] = f"obs://{bucket}/{prefix}/{record['archive']}"

    monkeypatch.setattr(capture, "create_obs_client", lambda *_: object())
    monkeypatch.setattr(capture, "create_transfer_config", object)
    monkeypatch.setattr(capture, "upload_archive", upload)
    monkeypatch.setattr(capture, "upload_json", lambda *_: None)
    manifest = upload_raw_artifacts(root, "job", "bucket", "https://obs.example.com", "region")
    assert manifest["status"] == "success"
    assert {case["case"] for case in manifest["cases"]} == {"config-a__performance", "config-b__performance"}
    assert set(uploaded) == {"config-a__performance/server.tar.gz", "config-b__performance/server.tar.gz"}


@pytest.mark.parametrize("options", [{}, {"enabled": True}, {"enabled": "TRUE", "duration": "12", "cases": "a, b"}])
def test_ci_options_and_environment_share_defaults(tmp_path, monkeypatch, options):
    for name in PROFILE_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    output, env_output, env_json = (tmp_path / name for name in ("output", "env", "env.json"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "capture",
            "configure",
            "--options-json",
            json.dumps(options),
            "--output",
            str(output),
            "--env-output",
            str(env_output),
            "--env-json",
            str(env_json),
        ],
    )
    capture.main()
    spec = ProfileSpec.from_options(options)
    emitted = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert ProfileSpec.from_options(json.loads(emitted["value"])) == spec
    assert emitted["parse"] == str(spec.enabled and spec.output == "parsed").lower()
    environment = dict(line.split("=", 1) for line in env_output.read_text().splitlines())
    assert json.loads(env_json.read_text())["profile_env"] == environment
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    assert ProfileSpec.from_env() == spec


@pytest.mark.parametrize("options", [{"max_size": "1GB"}, {"enabled": "yes"}, {"duration": 1.5}])
def test_options_reject_removed_fields_and_invalid_types(options):
    with pytest.raises(ValueError):
        ProfileSpec.from_options(options)


@pytest.mark.parametrize("enabled", [False, True])
def test_multi_node_template_expands_the_same_profile_environment(enabled):
    root = Path(__file__).resolve().parents[3]
    template = jinja2.Template((root / "tests/e2e/common/multi_node/lws.yaml.jinja2").read_text())
    environment = ProfileSpec(enabled=enabled).environment()
    documents = yaml.safe_load_all(template.render(profile_env=environment, log_prefix="/tmp/job", soc_version="a3"))
    templates = next(documents)["spec"]["leaderWorkerTemplate"]
    for role in ("leaderTemplate", "workerTemplate"):
        variables = {item["name"]: item["value"] for item in templates[role]["spec"]["containers"][0]["env"]}
        assert all(variables[name] == value for name, value in environment.items())
        assert variables["ASCEND_PROFILE_ROOT"] == "/tmp/job/profile_artifact"


@pytest.mark.parametrize("hardware", ["a2", "a3", "a3_560t", "a5", "310p"])
def test_schedule_workflows_use_shared_profile_configuration(hardware):
    root = Path(__file__).resolve().parents[3]
    workflow = yaml.safe_load((root / f".github/workflows/schedule_nightly_test_{hardware}.yaml").read_text())
    jobs = workflow["jobs"]
    configure = next(step for step in jobs["setup-vars"]["steps"] if step.get("id") == "profile-options")
    assert "tools.profiling.capture configure" in configure["run"]
    for job in jobs.values():
        if "profile_options_json" in job.get("with", {}):
            assert job["with"]["profile_options_json"] == "${{ needs.setup-vars.outputs.profile_options }}"
        is_parser = job.get("uses") == "./.github/workflows/_e2e_profile_parse.yaml"
        if "profile_options_json" in job.get("with", {}) or is_parser:
            for name in ("HW_OBS_PROFILE_AK", "HW_OBS_PROFILE_SK"):
                assert job["secrets"][name] == "${{ secrets." + name + " }}"
            if is_parser:
                assert "OBS_ACCESS_KEY_ID" not in job["secrets"]
                assert "OBS_SECRET_ACCESS_KEY" not in job["secrets"]
            else:
                assert job["secrets"]["OBS_ACCESS_KEY_ID"] == "${{ secrets.OBS_ACCESS_KEY_ID }}"
                assert job["secrets"]["OBS_SECRET_ACCESS_KEY"] == "${{ secrets.OBS_SECRET_ACCESS_KEY }}"
        assert "profile_enabled" not in job.get("with", {})
