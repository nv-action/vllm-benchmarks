"""Private combined-PR validation must test source, not image-bundled tools."""

import importlib.util
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
PR_REF = "${{ inputs.vllm_ascend_ref || github.sha }}"
IMAGE_DIGEST = "sha256:6d9d0340be9328915a33162a06b440652ecb730c8abff7bd0a123ae000600973"


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8-sig"), Loader=yaml.BaseLoader)


def test_validation_workflows_have_no_automatic_triggers():
    for path in WORKFLOWS.glob("*.y*ml"):
        assert not {"schedule", "push", "pull_request", "pull_request_target", "workflow_run"}.intersection(
            workflow(path.name)["on"]
        )


@pytest.mark.parametrize("soc", ["a3", "a3_560t"])
def test_benchmark_and_parser_use_same_image_and_source(soc):
    jobs = workflow(f"schedule_nightly_test_{soc}.yaml")["jobs"]
    images = set()
    for job in jobs.values():
        if job.get("uses", "").endswith(
            (
                "_e2e_profile_parse.yaml",
                "_e2e_nightly_multi_node.yaml",
                "_e2e_nightly_single_node.yaml",
                "_e2e_nightly_single_node_560t.yaml",
            )
        ):
            images.add(job["with"]["image"])
            if job["uses"].endswith("_e2e_profile_parse.yaml"):
                assert job["with"]["ref"] == PR_REF
    assert len(images) == 1 and images.pop().endswith("@" + IMAGE_DIGEST)
    checkout = next(step for step in jobs["setup-vars"]["steps"] if step.get("name") == "Checkout profiling tools")
    assert checkout["with"]["ref"] == PR_REF


def test_multi_node_source_and_logs_are_isolated():
    data = workflow("_e2e_nightly_multi_node.yaml")
    assert data["on"]["workflow_call"]["inputs"]["vllm_ascend_remote_url"]["default"] == (
        "https://github.com/nv-action/vllm-benchmarks.git"
    )
    steps = next(iter(data["jobs"].values()))["steps"]
    assert next(step for step in steps if step.get("name") == "Checkout code")["with"]["ref"] == PR_REF
    template = (ROOT / "tests/e2e/common/multi_node/lws.yaml.jinja2").read_text()
    assert template.count('bash "$LOG_PREFIX/validation-source/run.sh"') == 2
    script = (ROOT / "tests/e2e/common/multi_node/run.sh").read_text()
    assert 'git fetch --depth 1 origin "$VLLM_ASCEND_REF"' in script
    assert "git clone --depth 1" not in script
    assert "verify_profile_source.py" in script
    assert (
        "${{ github.run_id }}/${{ github.run_attempt }}"
        in workflow("schedule_nightly_test_a3.yaml")["env"]["ASCEND_LOG_PREFIX"]
    )


@pytest.mark.parametrize(
    "log,success",
    [
        ("VALIDATION_RUN_FINISHED:/test/run:0", True),
        ("VALIDATION_RUN_FINISHED:/test/run:1", False),
        ("fatal: early EOF", False),
        ("FAIL_TEST", False),
    ],
)
def test_non_aop_log_stream_requires_successful_completion(log, success, tmp_path):
    steps = next(iter(workflow("_e2e_nightly_multi_node.yaml")["jobs"].values()))["steps"]
    stream = next(step["run"] for step in steps if step.get("id") == "stream_logs")
    start = stream.index('kubectl logs -f "$LEADER_POD" -n "$NAMESPACE" | tee')
    script = stream[start:].split("\nfi\n", 1)[0]
    environment = {
        **os.environ,
        "LEADER_POD": "leader",
        "NAMESPACE": "test",
        "LOG_PREFIX": "/test/run",
        "LEADER_TMP": str(tmp_path / "leader.log"),
        "FAIL_TAG": "FAIL_TEST",
        "TEST_LOG": log,
    }
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", 'kubectl() { printf "%s\\n" "$TEST_LOG"; }\n' + script],
        env=environment,
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == success, result.stderr


@pytest.mark.parametrize("correct", [True, False])
def test_source_verifier_checks_actual_checkout_and_module_origins(correct, monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location("verify", WORKFLOWS / "scripts/verify_profile_source.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module.subprocess, "check_output", lambda *args, **kwargs: "test-sha\n")
    checked = []

    def find_spec(name):
        checked.append(name)
        return SimpleNamespace(origin=str((tmp_path if correct else Path("/image")) / name))

    monkeypatch.setattr(module.importlib.util, "find_spec", find_spec)
    if correct:
        module.verify_source(tmp_path, "test-sha")
        assert set(checked) == {
            "tools.profiling.capture",
            "tools.profiling.steady_state",
            "tools.profiling.parse",
            "tools.aisbench",
            "vllm_ascend",
        }
    else:
        with pytest.raises(RuntimeError, match="requested checkout"):
            module.verify_source(tmp_path, "test-sha")
