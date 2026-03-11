from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
from pathlib import Path
import re
import subprocess
import sys


_COMMIT_RANGE_RE = re.compile(
    r"^\*\*Commit range:\*\* `([0-9a-f]{40})`\.\.\.`([0-9a-f]{40})`$",
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


@dataclass(frozen=True)
class PrMetadata:
    old_commit: str
    new_commit: str


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


@dataclass(frozen=True)
class ActionDecision:
    action: str
    phase: str
    reason: str


@dataclass(frozen=True)
class FixupOutcome:
    result: str
    phase: str


class GitHubCliAdapter:
    def __init__(self, runner=None):
        self._runner = runner or self._run

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
            ]
        )

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
                "databaseId,workflowName,headSha,status,conclusion,url",
                "-L",
                "20",
            ]
        )
        runs = json.loads(output)
        for run in runs:
            if run.get("workflowName") != "E2E-Full":
                continue
            if run.get("headSha") != head_sha:
                continue
            if run.get("status") != "completed":
                continue
            return {
                "run_id": str(run["databaseId"]),
                "head_sha": str(run["headSha"]),
                "conclusion": str(run["conclusion"]),
                "run_url": str(run["url"]),
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
        )

    def _load_all(self) -> dict[str, dict[str, str | int]]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def _save_all(self, data: dict[str, dict[str, str | int]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))


class OrchestratorService:
    def __init__(self, store: Main2MainStateStore, github: GitHubCliAdapter):
        self.store = store
        self.github = github

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
            )
        elif decision.action == "create_manual_review":
            self.github.create_manual_review_issue(
                repo=repo,
                title="main2main: manual review needed",
                body=(
                    "Main2Main automation exhausted all fixup phases.\n\n"
                    f"PR: #{pr_number}\n"
                    f"Run: {e2e_result['run_url']}\n"
                    f"Commit range: {state.old_commit}...{state.new_commit}\n"
                ),
            )
        return {
            "action": decision.action,
            "phase": decision.phase,
            "reason": decision.reason,
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
            return {"action": "advance_phase", "phase": updated.phase, "reason": "changes pushed"}

        updated = self.store.update_after_no_change_fixup(
            repo=repo,
            pr_number=pr_number,
            expected_head_sha=state.head_sha,
        )
        if state.phase == "3":
            self.github.create_manual_review_issue(
                repo=repo,
                title="main2main: manual review needed",
                body=(
                    "Main2Main automation completed phase 3 with no code changes.\n\n"
                    f"PR: #{pr_number}\n"
                    f"Fixup run: https://github.com/{repo}/actions/runs/{fixup_run_id}\n"
                    f"Commit range: {state.old_commit}...{state.new_commit}\n"
                ),
            )
            return {
                "action": "create_manual_review",
                "phase": updated.phase,
                "reason": "phase 3 completed without code changes",
            }
        return {
            "action": "advance_phase",
            "phase": updated.phase,
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

    reconcile = subparsers.add_parser("reconcile")
    reconcile.add_argument("--state-file", required=True)
    reconcile.add_argument("--repo", required=True)
    reconcile.add_argument("--pr-number", required=True, type=int)

    return parser


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
        service = OrchestratorService(store, GitHubCliAdapter())
        result = service.reconcile(args.repo, args.pr_number)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0

    if args.command == "apply-fixup-outcome":
        service = OrchestratorService(store, GitHubCliAdapter())
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
