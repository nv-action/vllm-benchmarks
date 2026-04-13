from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any


def load_summary(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def extract_bisect_test_cmd(summary: dict[str, Any]) -> str:
    failed_test_cases = summary.get("failed_test_cases") or []
    if failed_test_cases:
        return f"pytest -sv {failed_test_cases[0]}"

    failed_test_files = summary.get("failed_test_files") or []
    if failed_test_files:
        return f"pytest -sv {failed_test_files[0]}"

    raise ValueError("No failed tests available to build bisect command")


def build_bisect_request_id(*, run_id: int | str, round_index: int) -> str:
    suffix = uuid.uuid4().hex[:8]
    return f"main2main-{run_id}-r{round_index}-{suffix}"


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def collect_new_commits(*, repo: Path, base_ref: str) -> list[dict[str, str]]:
    log_output = _run_git(
        repo,
        "log",
        "--reverse",
        "--format=%H%x1f%B%x1e",
        f"{base_ref}..HEAD",
    )
    commits: list[dict[str, str]] = []
    for record in log_output.split("\x1e"):
        record = record.strip()
        if not record:
            continue
        sha, message = record.split("\x1f", 1)
        commits.append(
            {
                "sha": sha.strip(),
                "short_sha": sha.strip()[:8],
                "message": message.strip(),
            }
        )
    return commits


def _format_commit_block(commits: list[dict[str, str]]) -> list[str]:
    if not commits:
        return ["- None"]

    lines: list[str] = []
    for commit in commits:
        lines.append(f"- `{commit['short_sha']}`")
        lines.append(commit["message"])
        lines.append("")
    if lines[-1] == "":
        lines.pop()
    return lines


def should_create_pr(commits: list[dict[str, str]]) -> bool:
    return bool(commits)


def render_pr_body(
    *,
    old_commit: str,
    new_commit: str,
    final_status: str,
    fix_rounds_used: int,
    bisect_rounds_used: int,
    commits: list[dict[str, str]],
) -> str:
    lines = [
        "## Summary",
        "",
        "Automated main2main adaptation against upstream vLLM changes.",
        "",
        "## Commit Range",
        "",
        f"`{old_commit}...{new_commit}`",
        "",
        "## Execution Result",
        "",
        f"- Final status: `{final_status}`",
        f"- Fix rounds used: `{fix_rounds_used}`",
        f"- Bisect-fix rounds used: `{bisect_rounds_used}`",
        "",
        "## Commits",
        "",
        *_format_commit_block(commits),
    ]
    return "\n".join(lines).rstrip() + "\n"


def render_manual_review_issue(
    *,
    pr_url: str,
    old_commit: str,
    new_commit: str,
    summary: dict[str, Any],
    bisect_summary: dict[str, Any] | None = None,
) -> str:
    code_bugs = summary.get("code_bugs") or []
    failed_test_files = summary.get("failed_test_files") or []
    failed_test_cases = summary.get("failed_test_cases") or []

    lines = [
        "## Summary",
        "",
        "main2main automation exhausted its fix and bisect budget.",
        "",
        "## Context",
        "",
        f"- Draft PR: {pr_url}",
        f"- Commit range: `{old_commit}`...`{new_commit}`",
        "",
        "## Remaining Failures",
        "",
        f"- Failed test files: `{len(failed_test_files)}`",
        f"- Failed test cases: `{len(failed_test_cases)}`",
        f"- Code bugs: `{len(code_bugs)}`",
    ]

    if code_bugs:
        lines.extend(["", "### Code Bugs", ""])
        for bug in code_bugs:
            error_type = bug.get("error_type", "UnknownError")
            error_message = bug.get("error_message", "")
            lines.append(f"- `{error_type}`: {error_message}")

    if bisect_summary:
        lines.extend(
            [
                "",
                "## Bisect Summary",
                "",
                f"- Status: `{bisect_summary.get('status', 'unknown')}`",
            ]
        )
        if bisect_summary.get("first_bad_commit"):
            lines.append(f"- First bad commit: `{bisect_summary['first_bad_commit']}`")
        if bisect_summary.get("first_bad_commit_url"):
            lines.append(f"- Commit URL: {bisect_summary['first_bad_commit_url']}")

    return "\n".join(lines).rstrip() + "\n"


def _run_gh_json(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def find_bisect_run(*, repo: str, request_id: str, workflow_name: str = "dispatch_main2main_bisect.yaml") -> dict[str, Any]:
    runs = _run_gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow_name,
            "--limit",
            "20",
            "--json",
            "databaseId,displayTitle,status,conclusion,workflowName,headBranch,event",
        ]
    )
    for run in runs:
        if request_id in (run.get("displayTitle") or ""):
            return run
    raise ValueError(f"No bisect run found for request_id={request_id}")


def poll_bisect_run(
    *,
    repo: str,
    run_id: int,
    timeout_seconds: int = 1800,
    poll_interval_seconds: int = 15,
) -> dict[str, Any]:
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        run = _run_gh_json(
            [
                "run",
                "view",
                str(run_id),
                "--repo",
                repo,
                "--json",
                "databaseId,status,conclusion,displayTitle",
            ]
        )
        if run.get("status") == "completed":
            return run
        time.sleep(poll_interval_seconds)
    raise TimeoutError(f"Timed out waiting for bisect run {run_id}")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Helper CLI for simplified main2main workflow.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    summary_parser = subparsers.add_parser("extract-bisect-test-cmd")
    summary_parser.add_argument("--summary", type=Path, required=True)

    request_id_parser = subparsers.add_parser("build-request-id")
    request_id_parser.add_argument("--run-id", required=True)
    request_id_parser.add_argument("--round-index", type=int, required=True)

    find_run_parser = subparsers.add_parser("find-bisect-run")
    find_run_parser.add_argument("--repo", required=True)
    find_run_parser.add_argument("--request-id", required=True)
    find_run_parser.add_argument("--workflow-name", default="dispatch_main2main_bisect.yaml")

    poll_parser = subparsers.add_parser("poll-bisect-run")
    poll_parser.add_argument("--repo", required=True)
    poll_parser.add_argument("--run-id", type=int, required=True)
    poll_parser.add_argument("--timeout-seconds", type=int, default=1800)
    poll_parser.add_argument("--poll-interval-seconds", type=int, default=15)

    commits_parser = subparsers.add_parser("collect-new-commits")
    commits_parser.add_argument("--repo", type=Path, required=True)
    commits_parser.add_argument("--base-ref", required=True)

    pr_parser = subparsers.add_parser("render-pr-body")
    pr_parser.add_argument("--old-commit", required=True)
    pr_parser.add_argument("--new-commit", required=True)
    pr_parser.add_argument("--final-status", required=True)
    pr_parser.add_argument("--fix-rounds-used", type=int, required=True)
    pr_parser.add_argument("--bisect-rounds-used", type=int, required=True)
    pr_parser.add_argument("--commits-json", type=Path, required=True)

    issue_parser = subparsers.add_parser("render-manual-review-issue")
    issue_parser.add_argument("--pr-url", required=True)
    issue_parser.add_argument("--old-commit", required=True)
    issue_parser.add_argument("--new-commit", required=True)
    issue_parser.add_argument("--summary-json", type=Path, required=True)
    issue_parser.add_argument("--bisect-json", type=Path)

    return parser


def main() -> None:
    args = _build_parser().parse_args()

    if args.command == "extract-bisect-test-cmd":
        print(extract_bisect_test_cmd(load_summary(args.summary)))
        return

    if args.command == "build-request-id":
        print(build_bisect_request_id(run_id=args.run_id, round_index=args.round_index))
        return

    if args.command == "find-bisect-run":
        print(json.dumps(find_bisect_run(repo=args.repo, request_id=args.request_id, workflow_name=args.workflow_name)))
        return

    if args.command == "poll-bisect-run":
        print(
            json.dumps(
                poll_bisect_run(
                    repo=args.repo,
                    run_id=args.run_id,
                    timeout_seconds=args.timeout_seconds,
                    poll_interval_seconds=args.poll_interval_seconds,
                )
            )
        )
        return

    if args.command == "collect-new-commits":
        print(json.dumps(collect_new_commits(repo=args.repo, base_ref=args.base_ref), ensure_ascii=False, indent=2))
        return

    if args.command == "render-pr-body":
        commits = json.loads(args.commits_json.read_text(encoding="utf-8"))
        print(
            render_pr_body(
                old_commit=args.old_commit,
                new_commit=args.new_commit,
                final_status=args.final_status,
                fix_rounds_used=args.fix_rounds_used,
                bisect_rounds_used=args.bisect_rounds_used,
                commits=commits,
            ),
            end="",
        )
        return

    if args.command == "render-manual-review-issue":
        summary = json.loads(args.summary_json.read_text(encoding="utf-8"))
        bisect_summary = None
        if args.bisect_json is not None:
            bisect_summary = json.loads(args.bisect_json.read_text(encoding="utf-8"))
        print(
            render_manual_review_issue(
                pr_url=args.pr_url,
                old_commit=args.old_commit,
                new_commit=args.new_commit,
                summary=summary,
                bisect_summary=bisect_summary,
            ),
            end="",
        )
        return

    raise AssertionError(f"Unhandled command: {args.command}")


if __name__ == "__main__":
    main()
