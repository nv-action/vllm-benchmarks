# SPDX-License-Identifier: Apache-2.0
"""CPU-only lifecycle tests; no GitHub or Kubernetes writes are performed."""

import importlib.util
import io
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import jinja2
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("lws_cleanup", ROOT / ".github/workflows/scripts/lws_cleanup.py")
assert SPEC is not None and SPEC.loader is not None
cleanup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cleanup)
PREFIX = cleanup.LABEL_PREFIX


@pytest.fixture
def args():
    return SimpleNamespace(
        command="sweep",
        repository="private-org/private-ci",
        repository_id="123",
        namespace="vllm-project",
        run_id="100",
        attempt="1",
        task_id="vllm-test-abc123",
        dry_run=False,
        wait_seconds=300,
    )


def resource(kind="LeaderWorkerSet", *, task="vllm-test-abc123", run="100", attempt="1", repository="123"):
    names = {"LeaderWorkerSet": task, "Service": f"{task}-leader", "StatefulSet": f"{task}-0", "Pod": f"{task}-0-1"}
    ownership = {
        f"{PREFIX}managed-by": cleanup.MANAGED_BY,
        f"{PREFIX}repository-id": repository,
        f"{PREFIX}run-id": run,
        f"{PREFIX}run-attempt": attempt,
        f"{PREFIX}task-id": task,
    }
    item = {
        "kind": kind,
        "metadata": {
            "name": names[kind],
            "namespace": "vllm-project",
            "uid": f"{names[kind]}-uid",
            "labels": ownership,
        },
    }
    if kind == "StatefulSet":
        item["metadata"]["labels"] = {cleanup.LWS_LABEL: task}
        item["spec"] = {"template": {"metadata": {"labels": ownership}}}
    return item


class FakeCluster:
    def __init__(self, resources):
        self.items = resources
        self.deleted = []
        self.wait_deleted = Mock()

    def resources(self, selector, *, include_children=False):
        expected = dict(part.split("=", 1) for part in selector.split(","))
        return [
            r
            for r in self.items
            if (include_children or r["kind"] in {"LeaderWorkerSet", "Service"})
            and all(cleanup.labels(r).get(k) == v for k, v in expected.items())
        ]

    def delete(self, item):
        self.deleted.append(item)
        self.items.remove(item)


@pytest.mark.parametrize("hardware,shared_volume", [("a2", ""), ("a3", ""), ("a5", "/mnt/weight")])
def test_template_labels_roots_and_pods_without_changing_workload(args, hardware, shared_volume):
    template = ROOT / "tests/e2e/common/multi_node/lws.yaml.jinja2"
    context = {
        "lws_name": args.task_id,
        "k8s_namespace": "custom-ci",
        "soc_version": hardware,
        "npu_per_node": "16" if hardware == "a3" else "8",
        "profile_env": {"PROFILE_TEST": "enabled"},
        "shared_volume_path": shared_volume,
    }
    render = jinja2.Template(template.read_text()).render
    unmanaged_lws, unmanaged_service = yaml.safe_load_all(render(**context))
    lws, service = yaml.safe_load_all(
        render(
            **context,
            ci_repository=args.repository,
            ci_repository_id=args.repository_id,
            ci_run_id=args.run_id,
            ci_run_attempt=args.attempt,
        )
    )

    expected = resource()["metadata"]["labels"]
    assert lws["metadata"]["labels"] == service["metadata"]["labels"] == expected
    assert lws["metadata"]["namespace"] == service["metadata"]["namespace"] == context["k8s_namespace"]
    assert lws["metadata"]["annotations"][f"{PREFIX}repository"] == args.repository
    assert service["spec"] == unmanaged_service["spec"]
    assert service["spec"]["selector"]["role"] == "leader"
    assert "labels" not in unmanaged_lws["metadata"]
    assert "labels" not in unmanaged_service["metadata"]

    for role in ("leaderTemplate", "workerTemplate"):
        pod = lws["spec"]["leaderWorkerTemplate"][role]
        assert all(pod["metadata"]["labels"][k] == v for k, v in expected.items())
        assert all(isinstance(v, str) for v in pod["metadata"]["labels"].values())
        assert pod["spec"] == unmanaged_lws["spec"]["leaderWorkerTemplate"][role]["spec"]
    assert lws["spec"]["leaderWorkerTemplate"]["leaderTemplate"]["metadata"]["labels"]["role"] == "leader"


@pytest.mark.parametrize("roots_exist", [True, False])
def test_local_cli_waits_for_current_task_children_without_github(args, monkeypatch, roots_exist):
    roots = [resource(), resource("Service")] if roots_exist else []
    children = [resource("Pod"), resource("StatefulSet")]
    others = [
        resource(task="vllm-other-123"),
        resource(task="vllm-rerun-123", attempt="2"),
        resource(repository="456"),
    ]
    cluster = FakeCluster(roots + children + others)
    wait_deleted = cleanup.Kubernetes.wait_deleted
    cluster.wait_deleted.side_effect = lambda *arguments: wait_deleted(cluster, *arguments)

    def finish_termination(seconds):
        for child in children:
            cluster.items.remove(child)

    sleep = Mock(side_effect=finish_termination)
    monkeypatch.setattr(cleanup.time, "sleep", sleep)
    github = Mock(side_effect=AssertionError("Local cleanup must not construct a GitHub client"))
    monkeypatch.setattr(cleanup, "GitHub", github)
    monkeypatch.setattr(cleanup, "Kubernetes", Mock(return_value=cluster))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "lws_cleanup.py",
            "cleanup",
            "--repository-id",
            args.repository_id,
            "--run-id",
            args.run_id,
            "--attempt",
            args.attempt,
            "--task-id",
            args.task_id,
        ],
    )
    assert cleanup.main() == 0
    assert cluster.deleted == roots
    assert cluster.items == others
    github.assert_not_called()
    sleep.assert_called_once_with(5)


@pytest.mark.parametrize("field", ["run-id", "run-attempt", "task-id"])
def test_missing_resource_identity_does_not_block_other_runs(args, field):
    invalid = resource(task="vllm-invalid-123")
    del invalid["metadata"]["labels"][f"{PREFIX}{field}"]
    valid = resource(run="200", task="vllm-valid-123")
    cluster = FakeCluster([invalid, valid])
    github = Mock()
    github.run_completed.return_value = True

    assert cleanup.sweep_completed_runs(args, github, cluster) == 1
    assert cluster.deleted == [valid]
    assert cluster.items == [invalid]
    github.run_completed.assert_called_once_with("200", "1")
    cluster.wait_deleted.assert_not_called()


def test_sweep_reclaims_old_runs_and_orphan_controllers_without_touching_active_attempt(args):
    orphan_sts = resource("StatefulSet", task="vllm-old-123", run="99")
    orphan_pod = resource("Pod", task="vllm-old-123", run="99")
    active = resource(task="vllm-rerun-123", attempt="2")
    roots = [resource(), resource("Service")]
    cluster = FakeCluster([orphan_pod, orphan_sts, active, *roots])
    github = Mock()
    github.run_completed.side_effect = lambda run, attempt: (run, attempt) != ("100", "2")

    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    assert cluster.deleted == [orphan_sts, orphan_pod, *roots]
    assert cluster.items == [active]
    assert github.run_completed.call_count == 3
    targets = cluster.wait_deleted.call_args.args[1]
    assert targets == {("99", "1", "vllm-old-123"), ("100", "1", args.task_id)}


def test_many_tasks_in_one_run_share_one_github_check(args):
    cluster = FakeCluster([resource(), resource("Service"), resource(task="vllm-other-123"), resource("Pod")])
    github = Mock()
    github.run_completed.return_value = True
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    github.run_completed.assert_called_once_with("100", "1")
    assert len(cluster.deleted) == 4
    cluster.wait_deleted.assert_called_once()


@pytest.mark.parametrize("kind", ["Pod", "StatefulSet", "Service"])
def test_later_sweep_reclaims_leftover_without_lws(args, kind):
    cluster = FakeCluster([resource(kind)])
    github = Mock()
    github.run_completed.return_value = True
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    assert [r["kind"] for r in cluster.deleted] == [kind]
    cluster.wait_deleted.assert_called_once()


def test_legacy_root_labels_work_without_job_id_or_expiry_checks(args):
    item = resource()
    item["metadata"]["labels"][f"{PREFIX}job-id"] = "42"
    item["metadata"]["annotations"] = {f"{PREFIX}expires-at": "2000-01-01T00:00:00Z"}
    cluster = FakeCluster([item])
    github = Mock()
    github.run_completed.return_value = False
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    assert not cluster.deleted


def test_private_repository_scope_and_dry_run(args):
    unlabelled = resource("Pod")
    unlabelled["metadata"]["labels"] = {}
    own = resource("Service")
    cluster = FakeCluster([resource(repository="456"), resource("StatefulSet", repository="456"), unlabelled, own])
    github = Mock()
    github.run_completed.return_value = True
    args.dry_run = True
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    assert not cluster.deleted
    cluster.wait_deleted.assert_not_called()
    args.dry_run = False
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    assert cluster.deleted == [own]


def test_no_resources_is_success_without_github_or_wait(args):
    cluster = FakeCluster([])
    github = Mock()
    assert cleanup.sweep_completed_runs(args, github, cluster) == 0
    github.run_completed.assert_not_called()
    cluster.wait_deleted.assert_not_called()


@pytest.mark.parametrize("status", ["completed", "in_progress", "queued", "waiting", "unknown"])
def test_github_uses_exact_attempt_in_executing_repository(args, monkeypatch, status):
    response = io.StringIO(json.dumps({"id": 100, "run_attempt": 1, "status": status}))
    request = Mock(return_value=response)
    monkeypatch.setattr(cleanup.urllib.request, "urlopen", request)
    assert cleanup.GitHub(args.repository, "token").run_completed("100", "1") == (status == "completed")
    assert (
        request.call_args.args[0].full_url
        == "https://api.github.com/repos/private-org/private-ci/actions/runs/100/attempts/1"
    )


@pytest.mark.parametrize("code,reason", [(403, "Forbidden"), (404, "Not Found"), (500, "Internal Server Error")])
def test_github_errors_preserve_resources_and_report_reason(args, monkeypatch, capsys, code, reason):
    token = "test-secret-token"
    headers = Message()
    headers["Authorization"] = token
    error = urllib.error.HTTPError(
        "https://api.github.com", code, reason, headers, io.BytesIO(b"private response body")
    )
    monkeypatch.setattr(cleanup.urllib.request, "urlopen", Mock(side_effect=error))
    cluster = FakeCluster([resource(), resource("Pod")])
    assert cleanup.sweep_completed_runs(args, cleanup.GitHub(args.repository, token), cluster) == 1
    assert not cluster.deleted
    cluster.wait_deleted.assert_not_called()
    stderr = capsys.readouterr().err
    assert f"Cannot verify run 100/1: HTTP Error {code}: {reason}" in stderr
    assert token not in stderr
    assert "private response body" not in stderr


def test_failure_for_one_run_does_not_block_other_completed_runs(args, capsys):
    cluster = FakeCluster([resource(), resource("Service"), resource(run="200", task="vllm-other-123")])
    github = Mock()
    github.run_completed.side_effect = [urllib.error.URLError("DNS lookup failed"), True]
    assert cleanup.sweep_completed_runs(args, github, cluster) == 1
    assert [r["metadata"]["name"] for r in cluster.deleted] == ["vllm-other-123"]
    assert github.run_completed.call_count == 2
    assert "DNS lookup failed" in capsys.readouterr().err


def test_statefulset_scope_comes_from_template_not_top_level_labels(monkeypatch):
    own = resource("StatefulSet")
    other = resource("StatefulSet", repository="456")
    other["metadata"]["labels"].update(resource()["metadata"]["labels"])
    cluster = cleanup.Kubernetes("vllm-project")
    command = Mock(side_effect=[json.dumps({"items": []}), json.dumps({"items": [own, other]})])
    monkeypatch.setattr(cluster, "command", command)
    selector = f"{PREFIX}managed-by={cleanup.MANAGED_BY},{PREFIX}repository-id=123"
    assert cluster.resources(selector, include_children=True) == [own]
    assert command.call_args_list[0].args[3] == selector


@pytest.mark.parametrize("kind", ["LeaderWorkerSet", "StatefulSet", "Service", "Pod"])
def test_delete_uses_atomic_uid_precondition_without_extra_get(monkeypatch, kind):
    run = Mock(return_value=SimpleNamespace(returncode=0, stdout=""))
    monkeypatch.setattr(cleanup.subprocess, "run", run)
    item = resource(kind)
    cleanup.Kubernetes("vllm-project").delete(item)
    run.assert_called_once()
    command = run.call_args.args[0]
    assert "delete" in command and "get" not in command
    options = json.loads(run.call_args.kwargs["input"])
    assert options["preconditions"] == {"uid": item["metadata"]["uid"]}
    assert options["propagationPolicy"] == "Background"


@pytest.mark.parametrize("reason", ["NotFound", "Forbidden", "Conflict"])
def test_only_not_found_is_ignored_on_delete(monkeypatch, reason):
    cluster = cleanup.Kubernetes("vllm-project")
    command = Mock(side_effect=cleanup.CleanupError(f"Error from server ({reason})"))
    monkeypatch.setattr(cluster, "command", command)
    if reason == "NotFound":
        cluster.delete(resource())
    else:
        with pytest.raises(cleanup.CleanupError, match=reason):
            cluster.delete(resource())
    command.assert_called_once()


@pytest.mark.parametrize("command", ["cleanup", "sweep"])
def test_failed_delete_does_not_block_other_deletions_or_spend_time_waiting(args, command, monkeypatch):
    cluster = FakeCluster([resource(), resource("Service")])
    delete = Mock(side_effect=[cleanup.CleanupError("Forbidden"), None])
    monkeypatch.setattr(cluster, "delete", delete)
    github = Mock()
    github.run_completed.return_value = True
    if command == "cleanup":
        result = cleanup.cleanup_current_task(args, cluster)
    else:
        result = cleanup.sweep_completed_runs(args, github, cluster)
    assert result == 1
    assert delete.call_count == 2
    cluster.wait_deleted.assert_not_called()


@pytest.mark.parametrize("kind", ["LeaderWorkerSet", "Service", "StatefulSet", "Pod"])
def test_wait_checks_all_resource_kinds_and_ignores_unrelated_active_run(monkeypatch, kind):
    pending = resource(kind)
    active = resource(run="200", task="vllm-active-123")
    cluster = cleanup.Kubernetes("vllm-project")
    resources = Mock(side_effect=[[pending, active], [active]])
    monkeypatch.setattr(cluster, "resources", resources)
    sleep = Mock()
    monkeypatch.setattr(cleanup.time, "sleep", sleep)
    cluster.wait_deleted("managed", {cleanup.identity(pending)}, 300)
    sleep.assert_called_once_with(5)


def test_wait_reports_leftovers_and_query_errors(monkeypatch):
    cluster = cleanup.Kubernetes("vllm-project")
    pod = resource("Pod")
    resources = Mock(return_value=[pod])
    monkeypatch.setattr(cluster, "resources", resources)
    with pytest.raises(cleanup.CleanupError, match="Pod/vllm-test-abc123-0-1"):
        cluster.wait_deleted("managed", {cleanup.identity(pod)}, 0)
    resources.side_effect = cleanup.CleanupError("API unavailable")
    with pytest.raises(cleanup.CleanupError, match="API unavailable"):
        cluster.wait_deleted("managed", {cleanup.identity(pod)}, 0)


def test_cleanup_monitors_all_multinode_workflows():
    directory = ROOT / ".github/workflows"
    workflow = yaml.load((directory / "lws_resource_cleanup.yaml").read_text(), Loader=yaml.BaseLoader)
    names = set()
    for path in directory.glob("*.y*ml"):
        caller = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
        if any(
            job.get("uses") == "./.github/workflows/_e2e_nightly_multi_node.yaml"
            for job in caller.get("jobs", {}).values()
        ):
            names.add(caller["name"])
    assert set(workflow["on"]["workflow_run"]["workflows"]) == names
    assert "schedule" not in workflow["on"]


def test_producer_renders_ownership_and_uses_one_cleanup():
    path = ROOT / ".github/workflows/_e2e_nightly_multi_node.yaml"
    workflow = yaml.load(path.read_text(), Loader=yaml.BaseLoader)
    steps = workflow["jobs"]["e2e"]["steps"]
    launch = next(step for step in steps if step.get("name") == "Launch cluster")
    render = launch["run"][: launch["run"].index("kubectl apply")]
    for variable in ("GITHUB_REPOSITORY", "GITHUB_REPOSITORY_ID", "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"):
        assert f"${variable}" in render
    assert '-D k8s_namespace="$NAMESPACE"' in render
    assert "GITHUB_TOKEN" not in launch["env"]
    cleanup_steps = [step for step in steps if "lws_cleanup.py cleanup" in step.get("run", "")]
    assert len(cleanup_steps) == 1
    assert "always()" in cleanup_steps[0]["if"]
    assert "!cancelled()" in workflow["jobs"]["e2e"]["if"]
    assert "--wait-seconds" not in cleanup_steps[0]["run"]
    assert int(cleanup_steps[0]["timeout-minutes"]) * 60 > cleanup.DELETE_WAIT_SECONDS
