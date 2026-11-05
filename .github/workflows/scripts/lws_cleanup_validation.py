# SPDX-License-Identifier: Apache-2.0
"""Restrict manual A3 validation to one run; reuse the production cleanup engine."""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import lws_cleanup as cleanup
import yaml

VALIDATION_LABEL = "ci.vllm.ai/validation"
VALIDATION_VALUE = "a3-cleanup"
VALIDATION_BRANCH = "test/a3-lws-cleanup-validation"
SOURCE_WORKFLOW = ".github/workflows/schedule_nightly_test_a3.yaml"
NAMESPACE = "vllm-project"
WAIT_SECONDS = cleanup.DELETE_WAIT_SECONDS


def task_name(run_id: str, attempt: str) -> str:
    return f"vllm-cleanup-{run_id}-{attempt}"


class ValidationCluster(cleanup.Kubernetes):
    def __init__(self, run_id: str, attempt: str):
        super().__init__(NAMESPACE)
        self.scope = {
            VALIDATION_LABEL: VALIDATION_VALUE,
            f"{cleanup.LABEL_PREFIX}run-id": run_id,
            f"{cleanup.LABEL_PREFIX}run-attempt": attempt,
            f"{cleanup.LABEL_PREFIX}task-id": task_name(run_id, attempt),
        }

    def resources(self, selector: str, *, include_children: bool = False) -> list[dict]:
        scope = ",".join(f"{key}={value}" for key, value in self.scope.items())
        return super().resources(",".join(filter(None, (selector, scope))), include_children=include_children)

    def delete(self, resource: dict) -> None:
        labels = cleanup.labels(resource)
        if any(labels.get(key) != value for key, value in self.scope.items()):
            raise cleanup.CleanupError("Resource is outside this validation run")
        super().delete(resource)


def isolate_manifest(manifest: Path, run_id: str, attempt: str) -> None:
    documents = list(yaml.safe_load_all(manifest.read_text()))
    name = task_name(run_id, attempt)
    if len(documents) != 2 or [d["kind"] for d in documents] != ["LeaderWorkerSet", "Service"]:
        raise cleanup.CleanupError("Expected one LWS and one Service")

    lws, service = documents
    group = lws["spec"]["leaderWorkerTemplate"]
    if lws["metadata"]["name"] != name or service["metadata"]["name"] != f"{name}-leader":
        raise cleanup.CleanupError("Unexpected validation resource names")
    if lws["spec"]["replicas"] != 1 or group["size"] != 2:
        raise cleanup.CleanupError("Validation must use exactly two pods")

    scripts = f"/root/.cache/cleanup-validation/{run_id}-{attempt}/scripts"
    for document in documents:
        document["metadata"].setdefault("labels", {})[VALIDATION_LABEL] = VALIDATION_VALUE

    for role in ("leaderTemplate", "workerTemplate"):
        template = group[role]
        template.setdefault("metadata", {}).setdefault("labels", {}).update(lws["metadata"]["labels"])
        container = template["spec"]["containers"][0]
        original = container["command"][-1]
        if "/root/.cache/tests/run.sh" not in original:
            raise cleanup.CleanupError("Unexpected workload entry point")
        container["command"][-1] = original.replace("/root/.cache/tests/run.sh", f"{scripts}/run.sh")

    manifest.write_text(yaml.safe_dump_all(documents, sort_keys=False))


def verify_source(github: cleanup.GitHub, run_id: str, attempt: str, repository_id: str) -> None:
    request = urllib.request.Request(
        f"https://api.github.com/repos/{github.repository}/actions/runs/{run_id}/attempts/{attempt}",
        headers={"Authorization": f"Bearer {github.token}", "Accept": "application/vnd.github+json"},
    )
    with urllib.request.urlopen(request, timeout=cleanup.REQUEST_TIMEOUT_SECONDS) as response:
        run = json.load(response)
    if (
        str(run["id"]) != run_id
        or str(run["run_attempt"]) != attempt
        or str(run["repository"]["id"]) != repository_id
        or run["head_branch"] != VALIDATION_BRANCH
        or run["path"].split("@", 1)[0] != SOURCE_WORKFLOW
        or run["status"] != "completed"
    ):
        raise cleanup.CleanupError("Source must be a completed run of the validation branch/workflow")
    print(f"Validated source: {run['html_url']} attempt={attempt} conclusion={run['conclusion']}")


def clean(args, github: cleanup.GitHub, cluster: ValidationCluster) -> int:
    verify_source(github, args.run_id, args.attempt, args.repository_id)
    sweep_args = SimpleNamespace(
        command="sweep",
        repository=args.repository,
        repository_id=args.repository_id,
        namespace=NAMESPACE,
        dry_run=args.dry_run,
        wait_seconds=WAIT_SECONDS,
    )
    return cleanup.sweep_completed_runs(sweep_args, github, cluster)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("prepare", "cleanup"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--attempt", required=True)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--manifest", type=Path, default=Path("lws.yaml"))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        cluster = ValidationCluster(args.run_id, args.attempt)
        if args.command == "prepare":
            selector = f"{VALIDATION_LABEL}={VALIDATION_VALUE},ci.vllm.ai/repository-id={args.repository_id}"
            unscoped = cleanup.Kubernetes(NAMESPACE)
            existing = unscoped.resources(selector, include_children=True)
            if existing:
                raise cleanup.CleanupError("Earlier validation resources remain; do not launch another case")
            isolate_manifest(args.manifest, args.run_id, args.attempt)
            return 0

        github = cleanup.GitHub(args.repository, os.environ.get("GITHUB_TOKEN", ""))
        return clean(args, github, cluster)
    except (
        cleanup.CleanupError,
        KeyError,
        TypeError,
        ValueError,
        OSError,
        yaml.YAMLError,
        subprocess.TimeoutExpired,
    ) as error:
        print(f"::error::Validation failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
