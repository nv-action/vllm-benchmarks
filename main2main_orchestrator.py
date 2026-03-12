from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.request
import uuid


_COMMIT_RANGE_RE = re.compile(
    r"^\*\*Commit range:\*\* `([0-9a-f]{40})`\.\.\.`([0-9a-f]{40})`$",
    re.MULTILINE,
)
_REGISTRATION_COMMENT_RE = re.compile(
    r"<!-- main2main-register\s*"
    r"pr_number=(\d+)\s*"
    r"branch=([^\n]+)\s*"
    r"head_sha=([0-9a-f]{40})\s*"
    r"old_commit=([0-9a-f]{40})\s*"
    r"new_commit=([0-9a-f]{40})\s*"
    r"phase=(2|3|done)\s*"
    r"-->",
    re.MULTILINE,
)

_FAILURE_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}

_ANALYSIS_SCRIPT_PATH = (
    Path(__file__).resolve().parent
    / ".claude"
    / "skills"
    / "main2main-error-analysis"
    / "scripts"
    / "extract_and_analyze.py"
)


@dataclass(frozen=True)
class PrMetadata:
    old_commit: str
    new_commit: str


@dataclass(frozen=True)
class RegistrationMetadata:
    pr_number: int
    branch: str
    head_sha: str
    old_commit: str
    new_commit: str
    phase: str


@dataclass(frozen=True)
class Main2MainState:
    repo: str
    pr_number: int
    branch: str
    head_sha: str
    old_commit: str
    new_commit: str
    phase: str
    status: str
    active_fixup_run_id: str | None = None


@dataclass(frozen=True)
class ActionDecision:
    action: str
    phase: str
    reason: str


@dataclass(frozen=True)
class FixupOutcome:
    result: str
    phase: str


def extract_e2e_failure_analysis(*, repo: str, run_id: str) -> dict[str, object]:
    completed = subprocess.run(
        [
            sys.executable,
            str(_ANALYSIS_SCRIPT_PATH),
            "--repo",
            repo,
            "--run-id",
            str(run_id),
            "--llm-output",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(completed.stdout)


def summarize_manual_review_issue(
    *,
    analysis: dict[str, object],
    state: Main2MainState,
    terminal_reason: str,
    e2e_run_url: str | None,
    e2e_run_id: str | None,
    fixup_run_id: str | None,
) -> str:
    base_url = os.environ["ANTHROPIC_BASE_URL"].rstrip("/")
    auth_token = os.environ["ANTHROPIC_AUTH_TOKEN"]
    payload = {
        "model": os.environ.get("MAIN2MAIN_ISSUE_MODEL", "claude-sonnet-4-5"),
        "max_tokens": 800,
        "messages": [
            {
                "role": "user",
                "content": (
                    "Summarize this terminal main2main failure for a GitHub issue.\n"
                    f"terminal_reason: {terminal_reason}\n"
                    f"pr_number: {state.pr_number}\n"
                    f"phase: {state.phase}\n"
                    f"e2e_run_id: {e2e_run_id}\n"
                    f"e2e_run_url: {e2e_run_url}\n"
                    f"fixup_run_id: {fixup_run_id}\n"
                    f"commit_range: {state.old_commit}...{state.new_commit}\n"
                    "Provide a concise issue body with summary, evidence, likely root cause, and next manual steps.\n"
                    f"analysis_json:\n{json.dumps(analysis, ensure_ascii=False)}"
                ),
            }
        ],
    }
    request = urllib.request.Request(
        f"{base_url}/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {auth_token}",
            "x-api-key": auth_token,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    with urllib.request.urlopen(request) as response:
        raw = json.loads(response.read().decode("utf-8"))
    for block in raw.get("content", []):
        if block.get("type") == "text":
            return str(block.get("text", "")).strip()
    raise ValueError("Claude gateway response did not contain text content")


class GitHubCliAdapter:
    def __init__(self, runner=None):
        self._runner = runner or self._run

    def list_open_main2main_pr_numbers(self, repo: str) -> list[int]:
        output = self._runner(
            [
                "gh",
                "pr",
                "list",
                "--repo",
                repo,
                "--state",
                "open",
                "--label",
                "main2main",
                "--json",
                "number",
            ]
        )
        prs = json.loads(output)
        return [int(pr["number"]) for pr in prs]

    def get_pr_context(self, repo: str, pr_number: int) -> dict[str, object]:
        output = self._runner(
            [
                "gh",
                "pr",
                "view",
                str(pr_number),
                "--repo",
                repo,
                "--json",
                "number,headRefOid,headRefName,body,labels,state",
            ]
        )
        raw = json.loads(output)
        return {
            "pr_number": raw["number"],
            "head_sha": raw["headRefOid"],
            "branch": raw["headRefName"],
            "state": raw["state"],
            "labels": [label["name"] for label in raw["labels"]],
            "metadata": parse_pr_metadata(raw["body"]),
            "body": raw["body"],
        }

    def get_registration_metadata(self, repo: str, pr_number: int) -> RegistrationMetadata:
        injected_comment = os.environ.get("MAIN2MAIN_TEST_REGISTRATION_COMMENT")
        if injected_comment:
            return parse_registration_comment(injected_comment)
        output = self._runner(
            [
                "gh",
                "api",
                f"repos/{repo}/issues/{pr_number}/comments",
            ]
        )
        comments = json.loads(output)
        for comment in reversed(comments):
            body = comment.get("body", "")
            if not isinstance(body, str):
                continue
            if "main2main-register" not in body:
                continue
            return parse_registration_comment(body)
        raise ValueError(f"registration metadata comment not found for PR #{pr_number}")

    def dispatch_fixup(
        self,
        *,
        repo: str,
        pr_number: int,
        branch: str,
        head_sha: str,
        run_id: str,
        run_url: str,
        conclusion: str,
        phase: str,
        old_commit: str,
        new_commit: str,
        dispatch_token: str,
    ) -> None:
        self._runner(
            [
                "gh",
                "workflow",
                "run",
                "main2main_auto.yaml",
                "--repo",
                repo,
                "-f",
                "mode=fixup",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"branch={branch}",
                "-f",
                f"head_sha={head_sha}",
                "-f",
                f"run_id={run_id}",
                "-f",
                f"run_url={run_url}",
                "-f",
                f"conclusion={conclusion}",
                "-f",
                f"phase={phase}",
                "-f",
                f"old_commit={old_commit}",
                "-f",
                f"new_commit={new_commit}",
                "-f",
                f"dispatch_token={dispatch_token}",
            ]
        )

    def find_latest_fixup_run(
        self,
        *,
        repo: str,
        dispatch_token: str,
    ) -> dict[str, str] | None:
        output = self._runner(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                "main2main_auto.yaml",
                "--json",
                "databaseId,status,conclusion,url,event,displayTitle",
                "-L",
                "20",
            ]
        )
        runs = json.loads(output)
        for run in runs:
            if run.get("event") != "workflow_dispatch":
                continue
            if dispatch_token not in str(run.get("displayTitle") or ""):
                continue
            return {
                "run_id": str(run["databaseId"]),
                "status": str(run["status"]),
                "conclusion": str(run.get("conclusion") or ""),
                "run_url": str(run["url"]),
            }
        return None

    def get_workflow_run(self, *, repo: str, run_id: str) -> dict[str, str]:
        output = self._runner(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--json",
                "databaseId,status,conclusion,url",
            ]
        )
        raw = json.loads(output)
        return {
            "run_id": str(raw["databaseId"]),
            "status": str(raw["status"]),
            "conclusion": str(raw.get("conclusion") or ""),
            "run_url": str(raw["url"]),
        }

    def mark_pr_ready(self, repo: str, pr_number: int) -> None:
        self._runner(
            [
                "gh",
                "pr",
                "ready",
                str(pr_number),
                "--repo",
                repo,
            ]
        )

    def create_manual_review_issue(self, *, repo: str, title: str, body: str) -> str:
        return self._runner(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                repo,
                "--title",
                title,
                "--label",
                "main2main",
                "--body",
                body,
            ]
        ).strip()

    def wait_for_e2e_full(self, *, repo: str, head_sha: str) -> dict[str, str] | None:
        output = self._runner(
            [
                "gh",
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                "pr_test_full.yaml",
                "--json",
                "databaseId,workflowName,headSha,status,conclusion,url,createdAt",
                "-L",
                "20",
            ]
        )
        runs = json.loads(output)
        matching_runs = []
        for run in runs:
            if run.get("workflowName") != "E2E-Full":
                continue
            if run.get("headSha") != head_sha:
                continue
            matching_runs.append(run)
        if not matching_runs:
            return None
        if any(run.get("status") != "completed" for run in matching_runs):
            return None
        latest_run = max(
            matching_runs,
            key=lambda run: (
                str(run.get("createdAt") or ""),
                int(run.get("databaseId") or 0),
            ),
        )
        if latest_run.get("status") != "completed":
            return None
        return {
            "run_id": str(latest_run["databaseId"]),
            "head_sha": str(latest_run["headSha"]),
            "conclusion": str(latest_run["conclusion"]),
            "run_url": str(latest_run["url"]),
        }
        return None

    def get_fixup_outcome(self, *, repo: str, run_id: str, phase: str) -> FixupOutcome:
        output = self._runner(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--json",
                "jobs",
            ]
        )
        raw = json.loads(output)
        jobs = raw.get("jobs", [])
        fixup_job = next((job for job in jobs if job.get("name") == "fixup"), None)
        if fixup_job is None:
            raise ValueError(f"fixup job not found in run {run_id}")
        job_output = self._runner(
            [
                "gh",
                "run",
                "view",
                run_id,
                "--repo",
                repo,
                "--job",
                str(fixup_job["databaseId"]),
            ]
        )
        return parse_fixup_job_output(job_output, phase=phase)

    @staticmethod
    def _run(args: list[str]) -> str:
        completed = subprocess.run(
            args,
            check=True,
            capture_output=True,
            text=True,
        )
        return completed.stdout


class Main2MainStateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)

    def register(self, state: Main2MainState) -> None:
        data = self._load_all()
        data[self._key(state.repo, state.pr_number)] = self._to_dict(state)
        self._save_all(data)

    def get(self, repo: str, pr_number: int) -> Main2MainState | None:
        data = self._load_all()
        raw = data.get(self._key(repo, pr_number))
        if raw is None:
            return None
        return self._from_dict(raw)

    def update_after_fixup(
        self,
        *,
        repo: str,
        pr_number: int,
        expected_head_sha: str,
        new_head_sha: str,
    ) -> Main2MainState:
        current = self.get(repo, pr_number)
        if current is None:
            raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")
        if current.head_sha != expected_head_sha:
            raise ValueError(
                f"stale fixup result for {expected_head_sha}, expected {current.head_sha}"
            )
        updated = apply_fixup_result(current, new_head_sha=new_head_sha)
        self.register(updated)
        return updated

    def update_after_no_change_fixup(
        self,
        *,
        repo: str,
        pr_number: int,
        expected_head_sha: str,
    ) -> Main2MainState:
        current = self.get(repo, pr_number)
        if current is None:
            raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")
        if current.head_sha != expected_head_sha:
            raise ValueError(
                f"stale fixup result for {expected_head_sha}, expected {current.head_sha}"
            )
        updated = apply_no_change_fixup_result(current)
        self.register(updated)
        return updated

    def mark_fixup_dispatched(
        self,
        *,
        repo: str,
        pr_number: int,
        run_id: str,
    ) -> Main2MainState:
        current = self.get(repo, pr_number)
        if current is None:
            raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")
        updated = replace(current, status="fixing", active_fixup_run_id=run_id)
        self.register(updated)
        return updated

    @staticmethod
    def _key(repo: str, pr_number: int) -> str:
        return f"{repo}#{pr_number}"

    @staticmethod
    def _to_dict(state: Main2MainState) -> dict[str, str | int]:
        return {
            "repo": state.repo,
            "pr_number": state.pr_number,
            "branch": state.branch,
            "head_sha": state.head_sha,
            "old_commit": state.old_commit,
            "new_commit": state.new_commit,
            "phase": state.phase,
            "status": state.status,
            "active_fixup_run_id": state.active_fixup_run_id,
        }

    @staticmethod
    def _from_dict(raw: dict[str, str | int]) -> Main2MainState:
        return Main2MainState(
            repo=str(raw["repo"]),
            pr_number=int(raw["pr_number"]),
            branch=str(raw["branch"]),
            head_sha=str(raw["head_sha"]),
            old_commit=str(raw["old_commit"]),
            new_commit=str(raw["new_commit"]),
            phase=str(raw["phase"]),
            status=str(raw["status"]),
            active_fixup_run_id=(
                str(raw["active_fixup_run_id"])
                if raw.get("active_fixup_run_id") is not None
                else None
            ),
        )

    def _load_all(self) -> dict[str, dict[str, str | int]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _save_all(self, data: dict[str, dict[str, str | int]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


class OrchestratorService:
    def __init__(
        self,
        store: Main2MainStateStore,
        github: GitHubCliAdapter,
        *,
        sleep_fn=time.sleep,
        token_factory=None,
    ):
        self.store = store
        self.github = github
        self.sleep_fn = sleep_fn
        self.token_factory = token_factory or (lambda: uuid.uuid4().hex)

    def _build_manual_review_issue(
        self,
        *,
        state: Main2MainState,
        pr_number: int,
        terminal_reason: str,
        e2e_run_url: str | None = None,
        e2e_run_id: str | None = None,
        fixup_run_id: str | None = None,
    ) -> dict[str, str]:
        if e2e_run_id is None and terminal_reason == "done_failure":
            raise ValueError("e2e_run_id is required for done_failure manual review")
        analysis = extract_e2e_failure_analysis(
            repo=state.repo,
            run_id=str(e2e_run_id),
        )
        body = summarize_manual_review_issue(
            analysis=analysis,
            state=state,
            terminal_reason=terminal_reason,
            e2e_run_url=e2e_run_url,
            e2e_run_id=e2e_run_id,
            fixup_run_id=fixup_run_id,
        )
        return {
            "title": "main2main: manual review needed",
            "body": body,
        }

    def _wait_for_dispatched_fixup_run(
        self,
        *,
        repo: str,
        dispatch_token: str,
        attempts: int = 10,
        interval_seconds: int = 2,
    ) -> dict[str, str] | None:
        for attempt in range(attempts):
            run = self.github.find_latest_fixup_run(
                repo=repo,
                dispatch_token=dispatch_token,
            )
            if run is not None:
                return run
            if attempt < attempts - 1:
                self.sleep_fn(interval_seconds)
        return None

    def reconcile(self, repo: str, pr_number: int) -> dict[str, str]:
        state = self.store.get(repo, pr_number)
        if state is None:
            raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")

        pr_context = self.github.get_pr_context(repo, pr_number)
        if pr_context["state"] != "OPEN":
            return {"action": "ignore", "reason": "pr is not open"}
        if "main2main" not in pr_context["labels"]:
            return {"action": "ignore", "reason": "pr is not marked main2main"}
        if pr_context["head_sha"] != state.head_sha:
            return {"action": "ignore", "reason": "pr head changed"}
        metadata = pr_context["metadata"]
        if not isinstance(metadata, PrMetadata):
            raise TypeError("invalid pr metadata")
        if metadata.old_commit != state.old_commit or metadata.new_commit != state.new_commit:
            return {"action": "ignore", "reason": "pr commit range changed"}

        e2e_result = self.github.wait_for_e2e_full(repo=repo, head_sha=state.head_sha)
        if e2e_result is None:
            return {"action": "wait", "reason": "e2e-full has not completed yet"}

        decision = decide_next_action(
            state,
            head_sha=e2e_result["head_sha"],
            conclusion=e2e_result["conclusion"],
        )
        if decision.action == "mark_ready":
            self.github.mark_pr_ready(repo, pr_number)
        elif decision.action == "dispatch_fixup":
            dispatch_token = self.token_factory()
            self.github.dispatch_fixup(
                repo=repo,
                pr_number=pr_number,
                branch=state.branch,
                head_sha=state.head_sha,
                run_id=e2e_result["run_id"],
                run_url=e2e_result["run_url"],
                conclusion=e2e_result["conclusion"],
                phase=state.phase,
                old_commit=state.old_commit,
                new_commit=state.new_commit,
                dispatch_token=dispatch_token,
            )
            fixup_run = self._wait_for_dispatched_fixup_run(
                repo=repo,
                dispatch_token=dispatch_token,
            )
            if fixup_run is None:
                raise ValueError(
                    f"unable to locate dispatched fixup run for token {dispatch_token}"
                )
            self.store.mark_fixup_dispatched(
                repo=repo,
                pr_number=pr_number,
                run_id=fixup_run["run_id"],
            )
        elif decision.action == "create_manual_review":
            issue = self._build_manual_review_issue(
                state=state,
                pr_number=pr_number,
                terminal_reason="done_failure",
                e2e_run_url=e2e_result["run_url"],
                e2e_run_id=e2e_result["run_id"],
            )
            self.store.register(replace(state, status="manual_review", active_fixup_run_id=None))
            self.github.create_manual_review_issue(repo=repo, title=issue["title"], body=issue["body"])
        return {
            "action": decision.action,
            "phase": decision.phase,
            "reason": decision.reason,
        }

    def run_once(self, repo: str) -> dict[str, object]:
        registered: list[int] = []
        fixup_outcomes: dict[str, dict[str, str]] = {}
        reconciled: dict[str, dict[str, str]] = {}

        for pr_number in self.github.list_open_main2main_pr_numbers(repo):
            state = self.store.get(repo, pr_number)
            if state is None:
                self.register_from_pr_comment(repo, pr_number)
                registered.append(pr_number)
                state = self.store.get(repo, pr_number)
            if state is None:
                raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")
            if state.status == "manual_review":
                continue
            if state.status == "fixing" and state.active_fixup_run_id is not None:
                run = self.github.get_workflow_run(repo=repo, run_id=state.active_fixup_run_id)
                if run["status"] == "completed":
                    fixup_outcomes[str(pr_number)] = self.apply_fixup_outcome(
                        repo,
                        pr_number,
                        state.active_fixup_run_id,
                    )
                continue
            reconciled[str(pr_number)] = self.reconcile(repo, pr_number)

        return {
            "registered": registered,
            "fixup_outcomes": fixup_outcomes,
            "reconciled": reconciled,
        }

    def register_from_pr_comment(self, repo: str, pr_number: int) -> dict[str, str]:
        metadata = self.github.get_registration_metadata(repo, pr_number)
        self.store.register(
            Main2MainState(
                repo=repo,
                pr_number=metadata.pr_number,
                branch=metadata.branch,
                head_sha=metadata.head_sha,
                old_commit=metadata.old_commit,
                new_commit=metadata.new_commit,
                phase=metadata.phase,
                status="waiting_e2e",
                active_fixup_run_id=None,
            )
        )
        return {
            "action": "register",
            "phase": metadata.phase,
            "reason": "registered from PR comment metadata",
        }

    def apply_fixup_outcome(self, repo: str, pr_number: int, fixup_run_id: str) -> dict[str, str]:
        state = self.store.get(repo, pr_number)
        if state is None:
            raise KeyError(f"unknown main2main PR: {repo}#{pr_number}")

        outcome = self.github.get_fixup_outcome(repo=repo, run_id=fixup_run_id, phase=state.phase)
        if outcome.result == "changes_pushed":
            pr_context = self.github.get_pr_context(repo, pr_number)
            new_head_sha = pr_context["head_sha"]
            if not isinstance(new_head_sha, str):
                raise TypeError("invalid PR head sha")
            updated = self.store.update_after_fixup(
                repo=repo,
                pr_number=pr_number,
                expected_head_sha=state.head_sha,
                new_head_sha=new_head_sha,
            )
            cleared = replace(updated, active_fixup_run_id=None)
            self.store.register(cleared)
            return {"action": "advance_phase", "phase": cleared.phase, "reason": "changes pushed"}

        updated = self.store.update_after_no_change_fixup(
            repo=repo,
            pr_number=pr_number,
            expected_head_sha=state.head_sha,
        )
        cleared = replace(updated, active_fixup_run_id=None)
        self.store.register(cleared)
        if state.phase == "3":
            issue = self._build_manual_review_issue(
                state=state,
                pr_number=pr_number,
                terminal_reason="phase3_no_changes",
                fixup_run_id=fixup_run_id,
            )
            self.github.create_manual_review_issue(
                repo=repo,
                title=issue["title"],
                body=issue["body"],
            )
            return {
                "action": "create_manual_review",
                "phase": cleared.phase,
                "reason": "phase 3 completed without code changes",
            }
        return {
            "action": "advance_phase",
            "phase": cleared.phase,
            "reason": "phase 2 completed without code changes",
        }


def parse_pr_metadata(body: str) -> PrMetadata:
    commit_match = _COMMIT_RANGE_RE.search(body)
    if commit_match is None:
        raise ValueError("PR body is missing main2main metadata")
    return PrMetadata(
        old_commit=commit_match.group(1),
        new_commit=commit_match.group(2),
    )


def parse_registration_comment(body: str) -> RegistrationMetadata:
    match = _REGISTRATION_COMMENT_RE.search(body)
    if match is None:
        raise ValueError("registration comment is missing main2main metadata")
    return RegistrationMetadata(
        pr_number=int(match.group(1)),
        branch=match.group(2),
        head_sha=match.group(3),
        old_commit=match.group(4),
        new_commit=match.group(5),
        phase=match.group(6),
    )


def parse_fixup_job_output(output: str, *, phase: str) -> FixupOutcome:
    if "No changes after phase" in output:
        return FixupOutcome(result="no_changes", phase=phase)
    if "fixes pushed" in output:
        return FixupOutcome(result="changes_pushed", phase=phase)
    raise ValueError("unable to determine fixup outcome from job output")


def normalize_conclusion(conclusion: str) -> str:
    if conclusion == "success":
        return "success"
    if conclusion in _FAILURE_CONCLUSIONS:
        return "failure"
    return "skip"


def decide_next_action(
    state: Main2MainState,
    *,
    head_sha: str,
    conclusion: str,
) -> ActionDecision:
    if head_sha != state.head_sha:
        return ActionDecision(
            action="ignore",
            phase=state.phase,
            reason=f"stale result for {head_sha}, expected {state.head_sha}",
        )

    normalized = normalize_conclusion(conclusion)
    if normalized == "success":
        return ActionDecision(
            action="mark_ready",
            phase=state.phase,
            reason="latest E2E-Full run succeeded",
        )
    if normalized == "failure":
        if state.phase == "done":
            return ActionDecision(
                action="create_manual_review",
                phase=state.phase,
                reason="all automated phases are exhausted",
            )
        return ActionDecision(
            action="dispatch_fixup",
            phase=state.phase,
            reason=f"phase {state.phase} requires another automated fix attempt",
        )
    return ActionDecision(
        action="ignore",
        phase=state.phase,
        reason=f"unsupported conclusion: {conclusion}",
    )


def apply_fixup_result(state: Main2MainState, *, new_head_sha: str) -> Main2MainState:
    if state.phase == "2":
        next_phase = "3"
    else:
        next_phase = "done"
    return replace(state, head_sha=new_head_sha, phase=next_phase, status="waiting_e2e")


def apply_no_change_fixup_result(state: Main2MainState) -> Main2MainState:
    if state.phase == "2":
        return replace(state, phase="3", status="waiting_e2e")
    return replace(state, phase="done", status="manual_review")


def run_loop(
    service: OrchestratorService,
    repo: str,
    *,
    interval_seconds: int,
    iterations: int | None = None,
    sleep_fn=time.sleep,
) -> list[dict[str, object]]:
    results: list[dict[str, object]] = []
    count = 0
    while iterations is None or count < iterations:
        results.append(service.run_once(repo))
        count += 1
        if iterations is not None and count >= iterations:
            break
        sleep_fn(interval_seconds)
    return results


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Minimal main2main orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    register = subparsers.add_parser("register")
    register.add_argument("--state-file", required=True)
    register.add_argument("--repo", required=True)
    register.add_argument("--pr-number", required=True, type=int)
    register.add_argument("--branch", required=True)
    register.add_argument("--head-sha", required=True)
    register.add_argument("--old-commit", required=True)
    register.add_argument("--new-commit", required=True)
    register.add_argument("--phase", required=True)

    decide = subparsers.add_parser("decide")
    decide.add_argument("--state-file", required=True)
    decide.add_argument("--repo", required=True)
    decide.add_argument("--pr-number", required=True, type=int)
    decide.add_argument("--head-sha", required=True)
    decide.add_argument("--conclusion", required=True)

    update_after_fixup = subparsers.add_parser("update-after-fixup")
    update_after_fixup.add_argument("--state-file", required=True)
    update_after_fixup.add_argument("--repo", required=True)
    update_after_fixup.add_argument("--pr-number", required=True, type=int)
    update_after_fixup.add_argument("--expected-head-sha", required=True)
    update_after_fixup.add_argument("--new-head-sha", required=True)

    apply_fixup_outcome = subparsers.add_parser("apply-fixup-outcome")
    apply_fixup_outcome.add_argument("--state-file", required=True)
    apply_fixup_outcome.add_argument("--repo", required=True)
    apply_fixup_outcome.add_argument("--pr-number", required=True, type=int)
    apply_fixup_outcome.add_argument("--fixup-run-id", required=True)

    register_from_pr_comment = subparsers.add_parser("register-from-pr-comment")
    register_from_pr_comment.add_argument("--state-file", required=True)
    register_from_pr_comment.add_argument("--repo", required=True)
    register_from_pr_comment.add_argument("--pr-number", required=True, type=int)

    run_once = subparsers.add_parser("run-once")
    run_once.add_argument("--state-file", required=True)
    run_once.add_argument("--repo", required=True)

    run_loop_parser = subparsers.add_parser("run-loop")
    run_loop_parser.add_argument("--state-file", required=True)
    run_loop_parser.add_argument("--repo", required=True)
    run_loop_parser.add_argument("--interval-seconds", required=True, type=int)
    run_loop_parser.add_argument("--iterations", type=int)

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--state-file", required=True)
    reconcile.add_argument("--repo", required=True)
    reconcile.add_argument("--pr-number", required=True, type=int)

    return parser


def _build_github_adapter():
    if os.environ.get("MAIN2MAIN_TEST_RUN_ONCE") == "1":
        class _TestRunOnceAdapter:
            def list_open_main2main_pr_numbers(self, repo: str) -> list[int]:
                assert repo == "nv-action/vllm-benchmarks"
                return [149]

            def get_registration_metadata(self, repo: str, pr_number: int) -> RegistrationMetadata:
                assert repo == "nv-action/vllm-benchmarks"
                assert pr_number == 149
                return RegistrationMetadata(
                    pr_number=149,
                    branch="main2main_auto_2026-03-11_02-02",
                    head_sha="0ac6428474c21eed75ceacac5b7fc04c58512a95",
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                    phase="2",
                )

            def get_pr_context(self, repo: str, pr_number: int) -> dict[str, object]:
                return {
                    "pr_number": pr_number,
                    "head_sha": "0ac6428474c21eed75ceacac5b7fc04c58512a95",
                    "branch": "main2main_auto_2026-03-11_02-02",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                    ),
                    "body": "",
                }

            def wait_for_e2e_full(self, *, repo: str, head_sha: str) -> dict[str, str] | None:
                return None

        return _TestRunOnceAdapter()
    return GitHubCliAdapter()


def _main(argv: list[str]) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    store = Main2MainStateStore(args.state_file)

    if args.command == "register":
        state = Main2MainState(
            repo=args.repo,
            pr_number=args.pr_number,
            branch=args.branch,
            head_sha=args.head_sha,
            old_commit=args.old_commit,
            new_commit=args.new_commit,
            phase=args.phase,
            status="waiting_e2e",
        )
        store.register(state)
        print(json.dumps(Main2MainStateStore._to_dict(state), indent=2, sort_keys=True))
        return 0

    if args.command == "update-after-fixup":
        updated = store.update_after_fixup(
            repo=args.repo,
            pr_number=args.pr_number,
            expected_head_sha=args.expected_head_sha,
            new_head_sha=args.new_head_sha,
        )
        print(json.dumps(Main2MainStateStore._to_dict(updated), indent=2, sort_keys=True))
        return 0

    if args.command == "reconcile":
        service = OrchestratorService(store, _build_github_adapter())
        result = service.reconcile(args.repo, args.pr_number)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "register-from-pr-comment":
        service = OrchestratorService(store, _build_github_adapter())
        result = service.register_from_pr_comment(args.repo, args.pr_number)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "run-once":
        service = OrchestratorService(store, _build_github_adapter())
        result = service.run_once(args.repo)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "run-loop":
        service = OrchestratorService(store, _build_github_adapter())
        result = run_loop(
            service,
            args.repo,
            interval_seconds=args.interval_seconds,
            iterations=args.iterations,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "apply-fixup-outcome":
        service = OrchestratorService(store, _build_github_adapter())
        result = service.apply_fixup_outcome(
            args.repo,
            args.pr_number,
            args.fixup_run_id,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    state = store.get(args.repo, args.pr_number)
    if state is None:
        raise SystemExit(f"unknown main2main PR: {args.repo}#{args.pr_number}")
    decision = decide_next_action(
        state,
        head_sha=args.head_sha,
        conclusion=args.conclusion,
    )
    print(
        json.dumps(
            {
                "action": decision.action,
                "phase": decision.phase,
                "reason": decision.reason,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(_main(sys.argv[1:]))
    except (KeyError, ValueError) as exc:
        raise SystemExit(str(exc))
