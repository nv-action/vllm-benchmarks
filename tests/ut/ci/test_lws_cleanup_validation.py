# SPDX-License-Identifier: Apache-2.0
"""Tests for validation isolation and wiring, without cluster or GitHub writes."""

import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / ".github/workflows/scripts"


@pytest.fixture
def validation(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("lws_cleanup_validation", SCRIPTS / "lws_cleanup_validation.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def manifest(tmp_path):
    path = tmp_path / "lws.yaml"
    template = ROOT / "tests/e2e/common/multi_node/lws.yaml.jinja2"
    path.write_text(
        jinja2.Template(template.read_text()).render(
            lws_name="vllm-cleanup-100-1", shared_volume_path="", soc_version="a3", profile_env={}
        )
    )
    return path


def test_isolation_preserves_real_workload_and_full_resources(validation, manifest):
    before = list(yaml.safe_load_all(manifest.read_text()))
    validation.isolate_manifest(manifest, "100", "1")
    after = list(yaml.safe_load_all(manifest.read_text()))
    for role in ("leaderTemplate", "workerTemplate"):
        old = before[0]["spec"]["leaderWorkerTemplate"][role]["spec"]["containers"][0]
        new = after[0]["spec"]["leaderWorkerTemplate"][role]["spec"]["containers"][0]
        assert new["resources"] == old["resources"]
        assert new["resources"]["requests"]["huawei.com/ascend-1980"] == 16
        assert new["env"] == old["env"]
        assert new["image"] == old["image"]
        assert "/cleanup-validation/100-1/scripts/run.sh" in new["command"][-1]
        assert "sleep" not in new["command"][-1]
    assert after[1]["spec"] == before[1]["spec"]
    assert all(d["metadata"]["labels"][validation.VALIDATION_LABEL] == "a3-cleanup" for d in after)


@pytest.mark.parametrize("size,replicas", [(3, 1), (2, 2)])
def test_refuse_more_than_two_pods(validation, manifest, size, replicas):
    documents = list(yaml.safe_load_all(manifest.read_text()))
    documents[0]["spec"]["replicas"] = replicas
    documents[0]["spec"]["leaderWorkerTemplate"]["size"] = size
    manifest.write_text(yaml.safe_dump_all(documents))
    with pytest.raises(validation.cleanup.CleanupError, match="exactly two"):
        validation.isolate_manifest(manifest, "100", "1")


def test_cleanup_selector_is_scoped_to_run_attempt_task_and_validation(validation, monkeypatch):
    cluster = validation.ValidationCluster("100", "2")
    command = Mock(return_value=json.dumps({"items": []}))
    monkeypatch.setattr(cluster, "command", command)
    cluster.resources("ci.vllm.ai/repository-id=123")
    selector = command.call_args.args[3]
    assert set(selector.split(",")) == {
        "ci.vllm.ai/repository-id=123",
        "ci.vllm.ai/validation=a3-cleanup",
        "ci.vllm.ai/run-id=100",
        "ci.vllm.ai/run-attempt=2",
        "ci.vllm.ai/task-id=vllm-cleanup-100-2",
    }


def test_delete_refuses_resources_outside_scope(validation, monkeypatch):
    cluster = validation.ValidationCluster("100", "1")
    command = Mock()
    monkeypatch.setattr(cluster, "command", command)
    with pytest.raises(validation.cleanup.CleanupError, match="outside"):
        cluster.delete({"kind": "Pod", "metadata": {"labels": {}}})
    command.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        {"id": 101},
        {"run_attempt": 2},
        {"repository": {"id": 456}},
        {"head_branch": "main"},
        {"path": ".github/workflows/unrelated.yaml"},
        {"status": "in_progress"},
    ],
)
def test_cleanup_refuses_wrong_source(validation, change, monkeypatch):
    run = {
        "id": 100,
        "run_attempt": 1,
        "repository": {"id": 123},
        "head_branch": validation.VALIDATION_BRANCH,
        "path": validation.SOURCE_WORKFLOW,
        "status": "completed",
    }
    github = Mock()
    response = io.StringIO(json.dumps(run | change))
    monkeypatch.setattr(validation.urllib.request, "urlopen", Mock(return_value=response))
    with pytest.raises(validation.cleanup.CleanupError, match="completed run"):
        validation.verify_source(github, "100", "1", "123")


def test_cleanup_uses_the_production_sweep_and_checks_remaining_pods(validation, monkeypatch):
    monkeypatch.setattr(validation, "verify_source", Mock())
    sweep = Mock(return_value=0)
    monkeypatch.setattr(validation.cleanup, "sweep_completed_runs", sweep)
    args = SimpleNamespace(run_id="100", attempt="1", repository="owner/repo", repository_id="123", dry_run=False)
    github, cluster = Mock(), Mock()
    assert validation.clean(args, github, cluster) == 0
    assert sweep.call_args.args[0].command == "sweep"
    assert sweep.call_args.args[1:3] == (github, cluster)
    assert sweep.call_args.args[0].wait_seconds == 300


def test_workflow_has_one_shared_cleanup_step_and_no_cluster_wide_jobs():
    directory = ROOT / ".github/workflows"
    workflow = yaml.load((directory / "_e2e_nightly_multi_node.yaml").read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["e2e"]
    steps = {step.get("name"): step for step in job["steps"]}
    assert "!cancelled()" in job["if"]
    launch = steps["Launch cluster"]
    assert launch["run"].index("-D ci_repository_id=") < launch["run"].index("kubectl apply")
    deletes = [s for s in job["steps"] if "lws_cleanup.py cleanup" in s.get("run", "")]
    assert len(deletes) == 1
    assert "always()" in deletes[0]["if"]
    assert "inputs.cleanup_validation && inputs.skip_local_cleanup" in deletes[0]["if"]
    assert deletes[0]["timeout-minutes"] == "10"
    assert "--wait-seconds" not in deletes[0]["run"]

    entry = yaml.load((directory / "schedule_nightly_test_a3.yaml").read_text(), Loader=yaml.BaseLoader)
    assert set(entry["on"]) == {"workflow_dispatch"}
    assert set(entry["jobs"]) == {"launch", "cleanup"}
    assert entry["permissions"]["actions"] == "read"
    assert entry["jobs"]["launch"]["with"]["size"] == "2"
    assert entry["jobs"]["cleanup"]["runs-on"] == "linux-aarch64-a3-800t-0"
    assert "kubectl taint" not in (directory / "schedule_nightly_test_a3.yaml").read_text()
