"""Execute the PR's cleanup and collection against isolated log directories."""

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
def test_cleanup_and_collection(tmp_path, workflow):
    steps = workflow_steps(workflow)
    cleanup = next(step for step in steps if step.get("name") == "Clear Ascend logs before tests")
    collect = next(step for step in steps if step.get("id") == "collect_ascend_logs")
    logs = tmp_path / "root/ascend/log"
    logs.mkdir(parents=True)
    (logs / "old-plog.log").write_text("image build log\n")
    output = tmp_path / "github-output"
    archive_path = tmp_path / "single-node-ascend-logs.tar.gz"
    env = {
        **os.environ,
        "GITHUB_OUTPUT": str(output),
        "GITHUB_RUN_ID": "123",
        "GITHUB_RUN_ATTEMPT": "2",
        "ARTIFACT_LABEL": "a3-560t/main:small test",
    }
    script = cleanup["run"].replace("/root/ascend/log", str(logs))
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, check=True)
    assert logs.is_dir()
    assert not (logs / "old-plog.log").exists()
    (logs / "current.log").write_text("model startup\n")
    pytest_logs = tmp_path / "tmp/test-logs"
    pytest_logs.mkdir(parents=True)
    (pytest_logs / "pytest-driven.log").write_text("1 passed\n")
    script = collect["run"].replace("/tmp/single-node-ascend-logs.tar.gz", str(archive_path))
    script = script.replace("-C / ", f"-C {tmp_path} ")
    subprocess.run(["bash", "-e", "-o", "pipefail", "-c", script], env=env, check=True)
    assert output.read_text().strip() == "name=single-node-a3-560t-main-small-test-123-2-ascend-logs"
    with tarfile.open(archive_path) as archive:
        names = archive.getnames()
        assert "root/ascend/log/current.log" in names
        assert "tmp/test-logs/pytest-driven.log" in names
        assert not any("old-plog" in name for name in names)


@pytest.mark.parametrize("workflow", WORKFLOWS)
def test_cleanup_precedes_tests_and_upload_follows_collection(workflow):
    steps = workflow_steps(workflow)
    names = [step.get("name") for step in steps]
    cleanup = names.index("Clear Ascend logs before tests")
    collect = names.index("Collect Ascend logs")
    for name in ("Run Pytest (py-driven)", "Run Pytest (YAML-driven)"):
        assert cleanup < names.index(name) < collect
    assert collect < names.index("Upload Ascend logs (GitHub Artifacts)")


def test_log_steps_are_identical_between_workflows():
    names = {"Clear Ascend logs before tests", "Collect Ascend logs", "Upload Ascend logs (GitHub Artifacts)"}
    log_steps = [[step for step in workflow_steps(name) if step.get("name") in names] for name in WORKFLOWS]
    assert log_steps[0] == log_steps[1]
