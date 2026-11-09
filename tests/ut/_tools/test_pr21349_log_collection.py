"""Execute the PR's workflow shell locally against isolated log directories."""

import os
import subprocess
import tarfile
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
WORKFLOWS = ("_e2e_nightly_single_node.yaml", "_e2e_nightly_single_node_560t.yaml")


def workflow_steps(name):
    workflow = yaml.safe_load((ROOT / ".github/workflows" / name).read_text())
    return next(iter(workflow["jobs"].values()))["steps"]


@pytest.mark.parametrize("workflow", WORKFLOWS)
@pytest.mark.parametrize("scenario", ["logs", "missing", "copy_failure", "tar_failure"])
def test_collection_shell(tmp_path, workflow, scenario):
    steps = workflow_steps(workflow)
    collect = next(step for step in steps if step.get("id") == "collect_ascend_logs")
    script = collect["run"]
    for source in ("/root/ascend/log", "/tmp/test-logs"):
        isolated = tmp_path / source.lstrip("/")
        if scenario != "missing":
            isolated.mkdir(parents=True)
            (isolated / ".hidden.log").write_text("runtime log\n")
            (isolated / "test.log").write_text("pytest output\n")
        script = script.replace(source, str(isolated))
    if scenario == "copy_failure":
        script = "cp() { return 1; }\n" + script
    elif scenario == "tar_failure":
        script = "tar() { return 1; }\n" + script
    output = tmp_path / "github-output"
    env = {
        **os.environ,
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_OUTPUT": str(output),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "ARTIFACT_LABEL": "a3-560t/main:small test",
    }
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, text=True, capture_output=True)
    if scenario == "tar_failure":
        assert result.returncode != 0
        assert not output.exists()
        return
    assert result.returncode == 0, result.stderr
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    assert values["name"].startswith("single-node-a3-560t-main-small-test-123-2-")
    with tarfile.open(values["path"]) as archive:
        assert "./log-environment.txt" in archive.getnames()
        if scenario == "logs":
            assert len([name for name in archive.getnames() if Path(name).name == ".hidden.log"]) == 2
            assert len([name for name in archive.getnames() if Path(name).name == "test.log"]) == 2
    if scenario != "logs":
        assert "::warning::" in result.stdout


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_upload_conditions_and_order(workflow):
    steps = workflow_steps(workflow)
    collect = next(step for step in steps if step.get("id") == "collect_ascend_logs")
    upload = next(step for step in steps if step["name"] == "Upload Ascend logs (GitHub Artifacts)")
    assert collect["if"] == "${{ !cancelled() }}"
    assert collect["continue-on-error"] is True
    assert upload["continue-on-error"] is True
    assert "steps.collect_ascend_logs.outcome == 'success'" in upload["if"]
    assert upload["with"]["retention-days"] == 7
    assert upload["with"]["compression-level"] == 0
    assert steps.index(collect) < steps.index(next(step for step in steps if step["name"] == "Capture test result"))


def test_collection_is_identical_between_workflows():
    collectors = [
        next(step for step in workflow_steps(name) if step.get("id") == "collect_ascend_logs") for name in WORKFLOWS
    ]
    assert collectors[0] == collectors[1]
