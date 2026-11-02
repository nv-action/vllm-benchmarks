# SPDX-License-Identifier: Apache-2.0
"""CPU-only lifecycle tests; no GitHub or Kubernetes writes are performed."""

import builtins
import copy
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SCRIPT = ROOT / ".github/workflows/scripts/lws_cleanup.py"
SPEC = importlib.util.spec_from_file_location("lws_cleanup", SCRIPT)
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
NOW = datetime(2026, 10, 23, 12, tzinfo=timezone.utc)
PREFIX = cleanup.LABEL_PREFIX
REPOSITORY = "vllm-project/vllm-ascend"
WORKFLOW = ".github/workflows/schedule_nightly_test_a3.yaml"


@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(
        command="sweep",
        repository=REPOSITORY,
        repository_id="123",
        namespace="vllm-project",
        run_id="100",
        attempt="1",
        task_id="vllm-test-abc123",
        manifest=tmp_path / "lws.yaml",
        workflow_ref=f"{REPOSITORY}/{WORKFLOW}@refs/heads/main",
        runner_name="runner-unique",
        timeout_minutes=120,
        dry_run=False,
        wait_seconds=0,
    )


def resource(kind="LeaderWorkerSet", task="vllm-test-abc123", attempt="1", job="10"):
    return {
        "kind": kind,
        "metadata": {
            "name": task if kind == "LeaderWorkerSet" else f"{task}-leader",
            "namespace": "vllm-project",
            "uid": f"{task}-{kind}-uid",
            "creationTimestamp": (NOW - timedelta(hours=1)).isoformat(),
            "labels": {
                f"{PREFIX}managed-by": cleanup.MANAGED_BY,
                f"{PREFIX}repository-id": "123",
                f"{PREFIX}run-id": "100",
                f"{PREFIX}run-attempt": attempt,
                f"{PREFIX}job-id": job,
                f"{PREFIX}task-id": task,
            },
            "annotations": {
                f"{PREFIX}repository": REPOSITORY,
                f"{PREFIX}workflow": WORKFLOW,
                f"{PREFIX}expires-at": (NOW + timedelta(hours=1)).isoformat(),
            },
        },
    }


def completed_job(conclusion="failure", attempt=1):
    return {
        "run_id": 100,
        "run_attempt": attempt,
        "status": "completed",
        "conclusion": conclusion,
        "completed_at": (NOW - timedelta(minutes=3)).isoformat(),
    }


class FakeCluster:
    def __init__(self, resources):
        self.items = resources
        self.deleted = []

    def resources(self, selector):
        labels = dict(part.split("=", 1) for part in selector.split(","))
        return [r for r in self.items if all(r["metadata"]["labels"].get(k) == v for k, v in labels.items())]

    def delete(self, item):
        self.deleted.append(item["metadata"]["name"])


@pytest.mark.parametrize("budget", [120, 240, 1440])
def test_stamp_rendered_manifest_before_apply(args, budget):
    template = ROOT / "tests/e2e/common/multi_node/lws.yaml.jinja2"
    args.manifest.write_text(
        jinja2.Template(template.read_text()).render(lws_name=args.task_id, profile_env={}, shared_volume_path="")
    )
    args.timeout_minutes = budget
    github = Mock()
    github.current_job.return_value = "10"
    cleanup.stamp_manifest(args, github, NOW)
    lws, service = yaml.safe_load_all(args.manifest.read_text())
    for item in (lws, service):
        assert item["metadata"]["labels"][f"{PREFIX}job-id"] == "10"
        assert item["metadata"]["labels"][f"{PREFIX}run-attempt"] == "1"
        assert cleanup.timestamp(item["metadata"]["annotations"][f"{PREFIX}expires-at"]) == NOW + timedelta(
            minutes=budget + cleanup.EXPIRY_MARGIN_MINUTES
        )
    assert service["metadata"]["labels"] == lws["metadata"]["labels"]
    # Stamping does not alter the workload or its existing leader selector.
    assert lws["spec"]["leaderWorkerTemplate"]["size"] == 2
    assert service["spec"]["selector"]["role"] == "leader"


def test_stamp_refuses_unidentifiable_job_without_modifying_manifest(args):
    original = yaml.safe_dump_all([resource(), resource("Service")])
    args.manifest.write_text(original)
    github = Mock()
    github.current_job.side_effect = cleanup.CleanupError("API unavailable")
    with pytest.raises(cleanup.CleanupError):
        cleanup.stamp_manifest(args, github, NOW)
    assert args.manifest.read_text() == original


@pytest.mark.parametrize("conclusion", ["success", "failure", "cancelled", "timed_out"])
def test_completed_job_reclaims_lws_and_service(args, conclusion):
    github = Mock()
    github.get.return_value = completed_job(conclusion)
    cluster = FakeCluster([resource(), resource("Service")])
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert cluster.deleted == [args.task_id, f"{args.task_id}-leader"]


def test_old_attempt_reclaimed_while_new_attempt_runs(args):
    github = Mock()
    github.get.side_effect = lambda path: (
        completed_job(attempt=1) if path == "jobs/10" else {"run_id": 100, "run_attempt": 2, "status": "in_progress"}
    )
    cluster = FakeCluster([resource(), resource(task="vllm-test-new123", attempt="2", job="20")])
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert cluster.deleted == [args.task_id]


@pytest.mark.parametrize("status", ["in_progress", "queued", "waiting", "pending", "requested"])
def test_active_job_is_preserved(args, status):
    github = Mock()
    github.get.return_value = {"run_id": 100, "run_attempt": 1, "status": status}
    cluster = FakeCluster([resource()])
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert not cluster.deleted


def test_completion_grace_period(args):
    github = Mock()
    github.get.return_value = completed_job() | {"completed_at": NOW.isoformat()}
    assert cleanup.deletion_reason(resource(), args, github, NOW) is None


@pytest.mark.parametrize("message", ["403", "404", "rate limit", "timeout"])
def test_api_errors_are_not_completion(args, message):
    github = Mock()
    github.get.side_effect = cleanup.CleanupError(message)
    cluster = FakeCluster([resource()])
    assert cleanup.reclaim(args, github, cluster, NOW) == 1
    assert not cluster.deleted


def test_expiry_works_without_github_and_does_not_block_other_resources(args):
    github = Mock()
    github.get.side_effect = cleanup.CleanupError("unavailable")
    expired = resource(task="vllm-test-old123")
    expired["metadata"]["annotations"][f"{PREFIX}expires-at"] = NOW.isoformat()
    cluster = FakeCluster([resource(), expired])
    assert cleanup.reclaim(args, github, cluster, NOW) == 1
    assert cluster.deleted == ["vllm-test-old123"]
    assert github.get.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        {"run_id": 101},
        {"run_attempt": 2},
        {"status": "unknown"},
        {"completed_at": None},
    ],
)
def test_unverifiable_job_is_preserved(args, change):
    github = Mock()
    github.get.return_value = completed_job() | change
    cluster = FakeCluster([resource()])
    assert cleanup.reclaim(args, github, cluster, NOW) == 1
    assert not cluster.deleted


@pytest.mark.parametrize("key,value", [("workflow", "other.yml"), ("repository", "other/repo"), ("expires-at", "bad")])
def test_invalid_ownership_is_preserved(args, key, value):
    item = resource()
    item["metadata"]["annotations"][f"{PREFIX}{key}"] = value
    cluster = FakeCluster([item])
    assert cleanup.reclaim(args, Mock(), cluster, NOW) == 1
    assert not cluster.deleted


@pytest.mark.parametrize("annotations", [None, {}, {f"{PREFIX}expires-at": None}])
def test_bad_metadata_does_not_block_other_tasks(args, annotations):
    invalid = resource(task="vllm-invalid-123")
    invalid["metadata"]["annotations"] = annotations
    cluster = FakeCluster([invalid, resource()])
    github = Mock()
    github.get.return_value = completed_job()
    assert cleanup.reclaim(args, github, cluster, NOW) == 1
    assert cluster.deleted == [args.task_id]


def test_unlabelled_and_other_repository_resources_are_ignored(args):
    legacy = resource()
    legacy["metadata"]["labels"] = {}
    other = resource()
    other["metadata"]["labels"][f"{PREFIX}repository-id"] = "456"
    cluster = FakeCluster([legacy, other])
    assert cleanup.reclaim(args, Mock(), cluster, NOW) == 0
    assert not cluster.deleted


@pytest.mark.parametrize("command", ["cleanup", "sweep"])
@pytest.mark.parametrize("expired", [False, True])
def test_private_repository_reclaims_only_its_own_resources(args, command, expired):
    args.repository = "private-org/private-ci"
    args.repository_id = "456"
    args.workflow_ref = f"{args.repository}/{WORKFLOW}@refs/heads/main"
    args.task_id = "vllm-private-abc123"
    args.command = command
    args.manifest.write_text(yaml.safe_dump_all([resource(task=args.task_id), resource("Service", task=args.task_id)]))
    github = Mock()
    github.current_job.return_value = "10"
    github.get.return_value = completed_job()
    cleanup.stamp_manifest(args, github, NOW)
    private_resources = list(yaml.safe_load_all(args.manifest.read_text()))
    # The other repository shares the namespace and even the run/job IDs.
    upstream_resources = [resource(), resource("Service")]
    items = upstream_resources + private_resources
    if expired:
        for item in items:
            item["metadata"]["annotations"][f"{PREFIX}expires-at"] = NOW.isoformat()
    cluster = FakeCluster(items)
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert cluster.deleted == [args.task_id, f"{args.task_id}-leader"]


def test_partial_creation_service_only_and_dry_run(args):
    github = Mock()
    github.get.return_value = completed_job()
    cluster = FakeCluster([resource("Service")])
    args.dry_run = True
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert not cluster.deleted
    args.dry_run = False
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert cluster.deleted == [f"{args.task_id}-leader"]


def test_local_cancel_cleanup_is_scoped_and_does_not_need_github(args):
    args.command = "cleanup"
    cluster = FakeCluster([resource(), resource("Service"), resource(task="vllm-other-123")])
    github = Mock()
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert cluster.deleted == [args.task_id, f"{args.task_id}-leader"]
    github.get.assert_not_called()
    args.attempt = "2"
    cluster.deleted.clear()
    assert cleanup.reclaim(args, github, cluster, NOW) == 0
    assert not cluster.deleted


def test_delete_uses_uid_precondition_and_foreground_gc(monkeypatch):
    item = resource()
    run = Mock(
        side_effect=[SimpleNamespace(returncode=0, stdout=json.dumps(item)), SimpleNamespace(returncode=0, stdout="")]
    )
    monkeypatch.setattr(cleanup.subprocess, "run", run)
    cleanup.Kubernetes("vllm-project").delete(item)
    command = run.call_args.args[0]
    assert "--raw" in command and command[-2:] == ["-f", "-"]
    assert "/apis/leaderworkerset.x-k8s.io/v1/namespaces/vllm-project/leaderworkersets/vllm-test-abc123" in command
    options = json.loads(run.call_args.kwargs["input"])
    assert options["preconditions"] == {"uid": item["metadata"]["uid"]}
    assert options["propagationPolicy"] == "Foreground"


def test_replaced_resource_is_not_deleted(monkeypatch):
    item = resource()
    replacement = copy.deepcopy(item)
    replacement["metadata"]["uid"] = "new-uid"
    cluster = cleanup.Kubernetes("vllm-project")
    command = Mock(return_value=json.dumps(replacement))
    monkeypatch.setattr(cluster, "command", command)
    with pytest.raises(cleanup.CleanupError, match="Ownership changed"):
        cluster.delete(item)
    assert command.call_count == 1


def test_concurrent_deletion_is_idempotent(monkeypatch):
    cluster = cleanup.Kubernetes("vllm-project")
    command = Mock(side_effect=[json.dumps(resource()), cleanup.CleanupError("NotFound"), ""])
    monkeypatch.setattr(cluster, "command", command)
    cluster.delete(resource())


def test_failed_delete_is_reported_and_does_not_stop_service_cleanup(args):
    cluster = FakeCluster([resource(), resource("Service")])
    cluster.delete = Mock(side_effect=[cleanup.CleanupError("Forbidden"), None])
    github = Mock()
    github.get.return_value = completed_job()
    assert cleanup.reclaim(args, github, cluster, NOW) == 1
    assert cluster.delete.call_count == 2


def test_wait_does_not_treat_api_error_or_live_pods_as_success(monkeypatch):
    cluster = cleanup.Kubernetes("vllm-project")
    monkeypatch.setattr(cluster, "resources", Mock(return_value=[]))
    command = Mock(return_value=json.dumps({"items": [{"metadata": {"name": "remaining-pod"}}]}))
    monkeypatch.setattr(cluster, "command", command)
    with pytest.raises(cleanup.CleanupError, match="still exist"):
        cluster.wait_deleted("managed", {"vllm-test-abc123"}, 0)
    command.side_effect = cleanup.CleanupError("Forbidden")
    with pytest.raises(cleanup.CleanupError, match="Forbidden"):
        cluster.wait_deleted("managed", {"vllm-test-abc123"}, 0)


def test_job_identity_paginates(monkeypatch):
    github = cleanup.GitHub(REPOSITORY, "test-token")
    get = Mock(
        side_effect=[
            {"jobs": [{"runner_name": "other", "status": "in_progress"}] * cleanup.PAGE_SIZE},
            {"jobs": [{"runner_name": "ours", "status": "in_progress", "id": 10}]},
        ]
    )
    monkeypatch.setattr(github, "get", get)
    assert github.current_job("100", "1", "ours") == "10"
    assert "page=2" in get.call_args.args[0]


def test_ambiguous_job_identity_rejected(monkeypatch):
    github = cleanup.GitHub(REPOSITORY, "test-token")
    monkeypatch.setattr(
        github, "get", Mock(return_value={"jobs": [{"runner_name": "ours", "status": "in_progress", "id": 10}] * 2})
    )
    monkeypatch.setattr(cleanup.time, "sleep", Mock())
    with pytest.raises(cleanup.CleanupError, match="exactly one"):
        github.current_job("100", "1", "ours")


def test_cleanup_import_does_not_require_regex(monkeypatch):
    original_import = builtins.__import__

    def import_without_regex(name, *args, **kwargs):
        if name == "regex":
            raise ModuleNotFoundError("nightly-cpu does not provide regex")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", import_without_regex)
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    assert module.positive_id("100") == "100"
