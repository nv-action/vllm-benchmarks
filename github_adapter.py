from __future__ import annotations

import json
import os
import subprocess


class GitHubCliAdapter:
    def __init__(self, runner=None):
        self._runner = runner or self._run

    def list_open_pr_numbers(self, repo: str, label: str = "main2main") -> list[int]:
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
                label,
                "--json",
                "number",
            ]
        )
        prs = json.loads(output)
        return [int(pr["number"]) for pr in prs]

    list_open_main2main_pr_numbers = list_open_pr_numbers

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
            "body": raw["body"],
        }

    def get_registration_metadata(self, repo: str, pr_number: int):
        from main2main_orchestrator import parse_registration_comment

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

    def get_fixup_outcome(self, *, repo: str, run_id: str, phase: str):
        from main2main_orchestrator import parse_fixup_job_output

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
