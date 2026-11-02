# Copyright (c) 2026 Huawei Technologies Co., Ltd.

import importlib.util
import json
from pathlib import Path

import pytest
import yaml


def load_probe():
    path = Path(__file__).resolve().parents[3] / ".github/workflows/scripts/probe_action_sync.py"
    spec = importlib.util.spec_from_file_location("probe_action_sync", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("location", ["workspace", "hidden", "temp", "commands"])
def test_versioned_payload_is_loadable_and_uses_the_intended_path(tmp_path, location):
    probe = load_probe()
    workspace = tmp_path / "_work/repo/repo"
    workspace.mkdir(parents=True)
    runner_temp = tmp_path / "_work/_temp"
    first = probe.stage(location, "v1", workspace, runner_temp, "run-1")
    manifest = Path(first["manifest_path"])
    payload = yaml.safe_load(manifest.read_text())
    assert payload["runs"]["steps"][0]["env"]["SYNC_LOADED_TOKEN"] == first["token"]
    assert "${{ github.action_path }}" in manifest.read_text()
    assert first["mode"] == "0o644"
    retry = probe.stage(location, "v2", workspace, runner_temp, "run-1")
    assert retry["previous_sha256"] == first["sha256"]
    assert retry["sha256"] != first["sha256"]
    assert retry["token"] != first["token"]


def test_reject_temp_mapping_that_would_probe_the_wrong_directory(tmp_path):
    with pytest.raises(ValueError, match="static uses path"):
        load_probe().target_directory("temp", tmp_path / "repo/repo", tmp_path / "different-temp")


@pytest.mark.parametrize(
    "first_ok,retry_token,expected",
    [
        (False, None, "unavailable-at-both-loads"),
        (True, None, "became-unavailable"),
        (False, "v1", "stale-v1-after-v2-write"),
        (True, "v1", "stale-v1-after-v2-write"),
        (False, "v2", "visible-after-intervening-step"),
        (True, "v2", "current-at-both-loads"),
    ],
)
def test_report_distinguishes_stale_sync_from_missing_directory(tmp_path, first_ok, retry_token, expected):
    phases = {phase: {"token": phase, "location": "workspace"} for phase in ("v1", "v2")}
    for phase, record in phases.items():
        (tmp_path / f"{phase}.json").write_text(json.dumps(record))
    first = {"outcome": "success" if first_ok else "failure", "outputs": {"token": "v1"}}
    retry = {"outcome": "success" if retry_token else "failure", "outputs": {"token": retry_token}}
    result = load_probe().report(tmp_path, first, retry, "a2")
    assert result["classification"] == expected
    assert json.loads((tmp_path / "result.json").read_text())["retry"] == retry


def test_rejects_successful_load_with_a_marker_from_another_run():
    phases = {phase: {"token": phase} for phase in ("v1", "v2")}
    with pytest.raises(ValueError, match="unexpected marker"):
        load_probe().classify(
            {"outcome": "failure"}, {"outcome": "success", "outputs": {"token": "another-run"}}, phases
        )
