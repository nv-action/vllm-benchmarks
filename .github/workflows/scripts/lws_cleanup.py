# SPDX-License-Identifier: Apache-2.0
"""Record CI ownership before provisioning, and reclaim finished LWS tasks.

Requires Python, PyYAML and kubectl. GitHub credentials are read only from
GITHUB_TOKEN; cluster credentials are handled by kubectl's normal kubeconfig.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

LABEL_PREFIX = "ci.vllm.ai/"
MANAGED_BY = "lws-cleanup"
LWS_RESOURCE = "leaderworkerset.x-k8s.io/v1"
WORKFLOWS = frozenset(
    {
        ".github/workflows/schedule_nightly_test_a2.yaml",
        ".github/workflows/schedule_nightly_test_a3.yaml",
        ".github/workflows/schedule_nightly_test_a5.yaml",
        ".github/workflows/schedule_weekly_test_a3.yaml",
    }
)
REQUEST_TIMEOUT_SECONDS = 15
PROCESS_TIMEOUT_SECONDS = 20
POLL_SECONDS = 2
IDENTITY_ATTEMPTS = 3
EXPIRY_MARGIN_MINUTES = 30
FINISHED_GRACE_SECONDS = 120
PAGE_SIZE = 100


class CleanupError(RuntimeError):
    pass


def timestamp(value: str) -> datetime:
    if not isinstance(value, str):
        raise CleanupError("Missing or invalid timestamp")
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise CleanupError("Timestamp must include a timezone")
    return result


def positive_id(value: str) -> str:
    if not re.fullmatch(r"[1-9][0-9]*", str(value)):
        raise CleanupError("Invalid CI identifier")
    return str(value)


class GitHub:
    def __init__(self, repository: str, token: str):
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository):
            raise CleanupError("Invalid repository")

        self.repository = repository
        self.token = token
        self.cache: dict[str, dict] = {}

    def get(self, endpoint: str) -> dict:
        if endpoint not in self.cache:
            request = urllib.request.Request(
                f"https://api.github.com/repos/{self.repository}/actions/{endpoint}",
                headers={
                    "Authorization": f"Bearer {self.token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
            )

            try:
                with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                    data = json.load(response)
                    if not isinstance(data, dict):
                        raise CleanupError(f"Invalid GitHub response for {endpoint}")
                    self.cache[endpoint] = data
            except (OSError, ValueError) as error:
                # Do not print headers, tokens or response bodies.
                raise CleanupError(f"Cannot verify GitHub {endpoint}: {type(error).__name__}") from error

        return self.cache[endpoint]

    def current_job(self, run_id: str, attempt: str, runner_name: str) -> str:
        for retry in range(IDENTITY_ATTEMPTS):
            self.cache.clear()
            matches = []
            page = 1

            while True:
                data = self.get(f"runs/{run_id}/attempts/{attempt}/jobs?per_page={PAGE_SIZE}&page={page}")
                jobs = data["jobs"]
                matches.extend(
                    job for job in jobs if job["runner_name"] == runner_name and job["status"] == "in_progress"
                )
                if len(jobs) < PAGE_SIZE:
                    break
                page += 1

            if len(matches) == 1:
                return positive_id(matches[0]["id"])
            if retry + 1 < IDENTITY_ATTEMPTS:
                time.sleep(POLL_SECONDS)

        raise CleanupError("Cannot identify exactly one running job; refusing to provision unowned resources")


def stamp_manifest(args, github: GitHub, now: datetime) -> None:
    """Write ownership into both resources before the first Kubernetes API write."""
    run_id, attempt = positive_id(args.run_id), positive_id(args.attempt)
    repository_id = positive_id(args.repository_id)
    workflow = args.workflow_ref.removeprefix(f"{args.repository}/").split("@", 1)[0]
    if workflow not in WORKFLOWS or args.timeout_minutes <= 0:
        raise CleanupError("Unsupported workflow or invalid job timeout")

    documents = list(yaml.safe_load_all(args.manifest.read_text()))
    if len(documents) != 2 or [doc["kind"] for doc in documents] != ["LeaderWorkerSet", "Service"]:
        raise CleanupError("Expected one LWS and its leader Service")

    task_id = documents[0]["metadata"]["name"]
    if not re.fullmatch(r"vllm-[a-z0-9-]{1,58}", task_id):
        raise CleanupError("Invalid LWS task name")
    if documents[1]["metadata"]["name"] != f"{task_id}-leader":
        raise CleanupError("Service does not belong to the LWS")

    job_id = github.current_job(run_id, attempt, args.runner_name)
    labels = {
        f"{LABEL_PREFIX}managed-by": MANAGED_BY,
        f"{LABEL_PREFIX}repository-id": repository_id,
        f"{LABEL_PREFIX}run-id": run_id,
        f"{LABEL_PREFIX}run-attempt": attempt,
        f"{LABEL_PREFIX}job-id": job_id,
        f"{LABEL_PREFIX}task-id": task_id,
    }

    expires = now + timedelta(minutes=args.timeout_minutes + EXPIRY_MARGIN_MINUTES)
    annotations = {
        f"{LABEL_PREFIX}repository": args.repository,
        f"{LABEL_PREFIX}workflow": workflow,
        f"{LABEL_PREFIX}expires-at": expires.isoformat(),
    }

    for document in documents:
        metadata = document["metadata"]
        if metadata.get("namespace") != args.namespace:
            raise CleanupError("Unexpected resource namespace")
        metadata.setdefault("labels", {}).update(labels)
        metadata.setdefault("annotations", {}).update(annotations)

    args.manifest.write_text(yaml.safe_dump_all(documents, sort_keys=False))


class Kubernetes:
    def __init__(self, namespace: str):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace):
            raise CleanupError("Invalid namespace")
        self.namespace = namespace

    def command(self, *arguments: str, body: dict | None = None) -> str:
        result = subprocess.run(
            ["kubectl", f"--request-timeout={REQUEST_TIMEOUT_SECONDS}s", "-n", self.namespace, *arguments],
            input=json.dumps(body) if body is not None else None,
            text=True,
            capture_output=True,
            timeout=PROCESS_TIMEOUT_SECONDS,
        )
        if result.returncode:
            raise CleanupError(f"kubectl failed: {result.stderr.strip()}")
        return result.stdout

    def resources(self, selector: str) -> list[dict]:
        return json.loads(self.command("get", "leaderworkerset,service", "-l", selector, "-o", "json"))["items"]

    def delete(self, resource: dict) -> None:
        """Use a UID precondition: a replaced object must never be deleted."""
        metadata = resource["metadata"]
        kind = resource["kind"]
        resource_type = "leaderworkerset" if kind == "LeaderWorkerSet" else "service"

        current = self.command("get", resource_type, metadata["name"], "--ignore-not-found", "-o", "json")
        if not current:
            return

        current_metadata = json.loads(current)["metadata"]
        if any(current_metadata.get(key) != metadata.get(key) for key in ("uid", "labels", "annotations")):
            raise CleanupError(f"Ownership changed for {kind}/{metadata['name']}; retry on the next sweep")

        if kind == "LeaderWorkerSet":
            path = f"/apis/{LWS_RESOURCE}/namespaces/{self.namespace}/leaderworkersets/{metadata['name']}"
        else:
            path = f"/api/v1/namespaces/{self.namespace}/services/{metadata['name']}"

        options = {
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"uid": metadata["uid"]},
            # Keep the owner visible until its children disappear, so later sweeps can retry.
            "propagationPolicy": "Foreground",
        }

        try:
            self.command("delete", "--raw", path, "-f", "-", body=options)
        except CleanupError:
            # Concurrent local/recovery cleanup is harmless only when the old UID is gone.
            remaining = self.command("get", resource_type, metadata["name"], "--ignore-not-found", "-o", "json")
            if remaining and json.loads(remaining)["metadata"]["uid"] == metadata["uid"]:
                raise

    def wait_deleted(self, selector: str, task_ids: set[str], seconds: int) -> None:
        deadline = time.monotonic() + seconds

        while True:
            resources = self.resources(selector)
            remaining = [r for r in resources if r["metadata"]["labels"].get(f"{LABEL_PREFIX}task-id") in task_ids]

            pods = []
            for task_id in task_ids:
                pods.extend(
                    json.loads(
                        self.command("get", "pods", "-l", f"leaderworkerset.sigs.k8s.io/name={task_id}", "-o", "json")
                    )["items"]
                )

            if not remaining and not pods:
                return
            if time.monotonic() >= deadline:
                raise CleanupError("Resources or LWS pods still exist; the next recovery run will retry")
            time.sleep(POLL_SECONDS)


def ownership(resource: dict, args) -> tuple[str, str, str, datetime]:
    metadata = resource["metadata"]
    labels, annotations = metadata["labels"], metadata["annotations"]
    task_id = labels[f"{LABEL_PREFIX}task-id"]
    expected_name = task_id if resource["kind"] == "LeaderWorkerSet" else f"{task_id}-leader"

    if (
        resource["kind"] not in {"LeaderWorkerSet", "Service"}
        or metadata["namespace"] != args.namespace
        or metadata["name"] != expected_name
        or not re.fullmatch(r"vllm-[a-z0-9-]{1,58}", task_id)
        or labels[f"{LABEL_PREFIX}managed-by"] != MANAGED_BY
        or labels[f"{LABEL_PREFIX}repository-id"] != args.repository_id
        or annotations[f"{LABEL_PREFIX}repository"] != args.repository
        or annotations[f"{LABEL_PREFIX}workflow"] not in WORKFLOWS
        or not metadata["uid"]
    ):
        raise CleanupError("Invalid resource ownership")

    run_id = positive_id(labels[f"{LABEL_PREFIX}run-id"])
    attempt = positive_id(labels[f"{LABEL_PREFIX}run-attempt"])
    job_id = positive_id(labels[f"{LABEL_PREFIX}job-id"])

    expires = timestamp(annotations[f"{LABEL_PREFIX}expires-at"])
    if expires <= timestamp(metadata["creationTimestamp"]):
        raise CleanupError("Invalid resource expiry")

    return run_id, attempt, job_id, expires


def deletion_reason(resource: dict, args, github: GitHub, now: datetime) -> str | None:
    run_id, attempt, job_id, expires = ownership(resource, args)
    if args.command == "cleanup":
        if run_id == args.run_id and attempt == args.attempt:
            return "local job cleanup"
        return None

    if now >= expires:
        return "hard deadline expired"

    job = github.get(f"jobs/{job_id}")
    if str(job["run_id"]) != run_id or str(job["run_attempt"]) != attempt:
        raise CleanupError("GitHub job does not match the resource's run/attempt")

    if job["status"] == "completed":
        completed = timestamp(job["completed_at"])
        if now >= completed + timedelta(seconds=FINISHED_GRACE_SECONDS):
            return f"job completed ({job['conclusion']})"
    elif job["status"] not in {"queued", "in_progress", "waiting", "pending", "requested"}:
        raise CleanupError("Unknown GitHub job status")

    return None


def reclaim(args, github: GitHub, cluster: Kubernetes, now: datetime) -> int:
    selector = f"{LABEL_PREFIX}managed-by={MANAGED_BY},{LABEL_PREFIX}repository-id={args.repository_id}"
    if args.command == "cleanup":
        selector += f",{LABEL_PREFIX}task-id={args.task_id}"

    failed = False
    tasks = set()
    for resource in cluster.resources(selector):
        name = resource["metadata"]["name"]
        try:
            reason = deletion_reason(resource, args, github, now)
            if reason is None:
                print(f"Keeping {name}: job is active or within completion grace period")
                continue

            print(f"{'Would delete' if args.dry_run else 'Deleting'} {resource['kind']}/{name}: {reason}")
            if not args.dry_run:
                cluster.delete(resource)
                tasks.add(resource["metadata"]["labels"][f"{LABEL_PREFIX}task-id"])
        except (CleanupError, KeyError, TypeError, ValueError, subprocess.TimeoutExpired) as error:
            print(f"::error::Could not reclaim {name}: {error}", file=sys.stderr)
            failed = True

    if tasks and args.wait_seconds:
        cluster.wait_deleted(selector, tasks, args.wait_seconds)

    return int(failed)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stamp", "cleanup", "sweep"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--repository-id", required=True, type=positive_id)
    parser.add_argument("--namespace", default="vllm-project")
    parser.add_argument("--run-id", type=positive_id)
    parser.add_argument("--attempt", type=positive_id)
    parser.add_argument("--task-id")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--workflow-ref")
    parser.add_argument("--runner-name")
    parser.add_argument("--timeout-minutes", type=int)
    parser.add_argument("--wait-seconds", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.command == "stamp" and any(
        getattr(args, name) is None
        for name in ("run_id", "attempt", "manifest", "workflow_ref", "runner_name", "timeout_minutes")
    ):
        parser.error("stamp requires run-id, attempt, manifest, workflow-ref, runner-name and timeout-minutes")
    if args.command == "cleanup" and not (args.run_id and args.attempt and args.task_id):
        parser.error("cleanup requires run-id, attempt and task-id")
    if args.task_id and not re.fullmatch(r"vllm-[a-z0-9-]{1,58}", args.task_id):
        parser.error("invalid task-id")

    try:
        github = GitHub(args.repository, os.environ.get("GITHUB_TOKEN", ""))
        now = datetime.now(timezone.utc)

        if args.command == "stamp":
            stamp_manifest(args, github, now)
            return 0
        return reclaim(args, github, Kubernetes(args.namespace), now)
    except (CleanupError, KeyError, TypeError, ValueError, OSError, yaml.YAMLError, subprocess.TimeoutExpired) as error:
        print(f"::error::LWS cleanup failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
