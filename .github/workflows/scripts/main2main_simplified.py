from __future__ import annotations

import argparse
import json
import subprocess
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


def collect_commit_range(*, repo: Path, start_ref: str, end_ref: str) -> list[dict[str, str]]:
    log_output = _run_git(
        repo,
        "log",
        "--reverse",
        "--format=%H%x1f%s%x1f%b%x1e",
        f"{start_ref}..{end_ref}",
    )
    commits: list[dict[str, str]] = []
    for record in log_output.split("\x1e"):
        if not record.strip():
            continue
        record = record.rstrip("\n")
        parts = record.split("\x1f", 2)
        if len(parts) == 2:
            sha, subject = parts
            body = ""
        elif len(parts) == 3:
            sha, subject, body = parts
        else:
            raise ValueError(f"Unexpected git log record format: {record!r}")
        commits.append(
            {
                "sha": sha.strip(),
                "subject": subject.strip(),
                "body": body.strip(),
            }
        )
    return commits


def append_round_commits_markdown(
    *,
    output_path: Path,
    phase: str,
    round_index: int,
    commits: list[dict[str, str]],
) -> None:
    if not commits:
        return

    lines: list[str] = []
    if output_path.exists() and output_path.read_text(encoding="utf-8").strip():
        lines.append("")
        lines.append("")
    lines.append(f"### phase: {phase}")
    lines.append(f"round: {round_index}")
    lines.append("")
    lines.append("git_commits:")
    for commit in commits:
        lines.append(f"- sha: `{commit['sha']}`")
        lines.append(f"  subject: {commit['subject']}")
        lines.append("  body:")
        body = commit["body"] or "(empty)"
        for body_line in body.splitlines():
            lines.append(f"    {body_line}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("a", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
        handle.write("\n")


def render_detect_prompt(*, work_repo_dir: str, vllm_dir: str, old_commit: str, new_commit: str) -> str:
    return (
        "\n".join(
            [
                "Use Main2Main skill to adapt vllm-benchmarks to the latest vLLM main branch.",
                "",
                "Context:",
                f"- Benchmark repo is checked out at ./{work_repo_dir}",
                f"- Upstream vLLM source is checked out at ./{vllm_dir}",
                f"- OLD_COMMIT={old_commit}",
                f"- NEW_COMMIT={new_commit}",
                "",
                "Requirements:",
                "- Analyze upstream diff and apply adaptation fixes",
                "- Update commit references if needed",
                "- Create a git commit if and only if you make valid code changes",
                "- Do not push",
                "- Do not create a PR",
            ]
        )
        + "\n"
    )


def render_fix_prompt(
    *,
    work_repo_dir: str,
    vllm_dir: str,
    old_commit: str,
    new_commit: str,
    round_index: int,
    log_path: str,
) -> str:
    return (
        "\n".join(
            [
                "Use Main2Main skill to fix the current main2main test failures.",
                f"test failures log is available at {log_path}",
                "",
                "Context:",
                f"- Benchmark repo is checked out at ./{work_repo_dir}",
                f"- Upstream vLLM source is checked out at ./{vllm_dir}",
                f"- OLD_COMMIT={old_commit}",
                f"- NEW_COMMIT={new_commit}",
                f"- Current round={round_index}",
                f"- Main2Main test log path={log_path}",
                "",
                "Requirements:",
                f"- Use {log_path} as the primary failure-analysis input",
                "- Fix code bugs in the benchmark repo",
                "- Modify code only",
                "- Create a git commit if and only if you make valid code changes",
                "- Do not push",
                "- Do not create a PR",
            ]
        )
        + "\n"
    )


def render_bisect_fix_prompt(
    *,
    work_repo_dir: str,
    vllm_dir: str,
    old_commit: str,
    new_commit: str,
    round_index: int,
    log_path: str,
    bisect_result_path: str,
) -> str:
    return (
        "\n".join(
            [
                "Use the main2main skill to fix the remaining main2main failures based on bisect results.",
                "The CI still failed after fix, so test failure log and bisect results are also provided.",
                "",
                "Context:",
                f"- Benchmark repo is checked out at ./{work_repo_dir}",
                f"- Upstream vLLM source is checked out at ./{vllm_dir}",
                f"- Main2Main test log path={log_path}",
                f"- Bisect result path={bisect_result_path}",
                f"- Round={round_index}",
                f"- OLD_COMMIT={old_commit}",
                f"- NEW_COMMIT={new_commit}",
                "",
                "Requirements:",
                "- Use the bisect result to produce a targeted fix",
                f"- Use {log_path} as the primary failure-analysis input",
                "- Modify code only",
                "- Create a git commit if and only if you make valid code changes",
                "- Do not push",
                "- Do not create a PR",
            ]
        )
        + "\n"
    )


def should_create_pr(commits: list[dict[str, str]]) -> bool:
    return bool(commits)


def render_pr_body(
    *,
    old_commit: str,
    new_commit: str,
    rounds_markdown: str,
) -> str:
    lines = [
        "Automated adaptation to upstream vLLM main branch changes.",
        f"Commit range: {old_commit}...{new_commit}",
        "",
    ]
    rounds_markdown = rounds_markdown.strip()
    if rounds_markdown:
        lines.append(rounds_markdown)
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


def find_bisect_run(
    *, repo: str, request_id: str, workflow_name: str = "dispatch_main2main_bisect.yaml"
) -> dict[str, Any]:
    runs = _run_gh_json(
        [
            "run",
            "list",
            "--repo",
            repo,
            "--workflow",
            workflow_name,
            "--limit",
            "100",
            "--json",
            "databaseId,displayTitle,status,conclusion,workflowName,headBranch,event,url",
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
                "databaseId,status,conclusion,displayTitle,url",
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

    commits_parser = subparsers.add_parser("collect-commit-range")
    commits_parser.add_argument("--repo", type=Path, required=True)
    commits_parser.add_argument("--start-ref", required=True)
    commits_parser.add_argument("--end-ref", required=True)

    append_round_parser = subparsers.add_parser("append-round-commits-markdown")
    append_round_parser.add_argument("--repo", type=Path, required=True)
    append_round_parser.add_argument("--output", type=Path, required=True)
    append_round_parser.add_argument("--phase", required=True, choices=["detect", "fix", "bisect"])
    append_round_parser.add_argument("--round", type=int, required=True)
    append_round_parser.add_argument("--start-ref", required=True)
    append_round_parser.add_argument("--end-ref", required=True)

    prompt_parser = subparsers.add_parser("render-prompt")
    prompt_parser.add_argument("--phase", required=True, choices=["detect", "fix", "bisect"])
    prompt_parser.add_argument("--output", type=Path, required=True)
    prompt_parser.add_argument("--work-repo-dir", required=True)
    prompt_parser.add_argument("--vllm-dir", required=True)
    prompt_parser.add_argument("--old-commit", required=True)
    prompt_parser.add_argument("--new-commit", required=True)
    prompt_parser.add_argument("--round", type=int)
    prompt_parser.add_argument("--log-path")
    prompt_parser.add_argument("--bisect-result-path")

    pr_parser = subparsers.add_parser("render-pr-body")
    pr_parser.add_argument("--old-commit", required=True)
    pr_parser.add_argument("--new-commit", required=True)
    pr_parser.add_argument("--rounds-md", type=Path, required=True)

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

    if args.command == "collect-commit-range":
        print(
            json.dumps(
                collect_commit_range(repo=args.repo, start_ref=args.start_ref, end_ref=args.end_ref),
                ensure_ascii=False,
                indent=2,
            )
        )
        return

    if args.command == "append-round-commits-markdown":
        commits = collect_commit_range(repo=args.repo, start_ref=args.start_ref, end_ref=args.end_ref)
        append_round_commits_markdown(
            output_path=args.output,
            phase=args.phase,
            round_index=args.round,
            commits=commits,
        )
        print(json.dumps({"count": len(commits)}, ensure_ascii=False))
        return

    if args.command == "render-prompt":
        if args.phase == "detect":
            text = render_detect_prompt(
                work_repo_dir=args.work_repo_dir,
                vllm_dir=args.vllm_dir,
                old_commit=args.old_commit,
                new_commit=args.new_commit,
            )
        elif args.phase == "fix":
            if args.round is None:
                raise ValueError("--round is required for phase=fix")
            if not args.log_path:
                raise ValueError("--log-path is required for phase=fix")
            text = render_fix_prompt(
                work_repo_dir=args.work_repo_dir,
                vllm_dir=args.vllm_dir,
                old_commit=args.old_commit,
                new_commit=args.new_commit,
                round_index=args.round,
                log_path=args.log_path,
            )
        else:
            if args.round is None:
                raise ValueError("--round is required for phase=bisect")
            if not args.log_path:
                raise ValueError("--log-path is required for phase=bisect")
            if not args.bisect_result_path:
                raise ValueError("--bisect-result-path is required for phase=bisect")
            text = render_bisect_fix_prompt(
                work_repo_dir=args.work_repo_dir,
                vllm_dir=args.vllm_dir,
                old_commit=args.old_commit,
                new_commit=args.new_commit,
                round_index=args.round,
                log_path=args.log_path,
                bisect_result_path=args.bisect_result_path,
            )
        args.output.write_text(text, encoding="utf-8")
        return

    if args.command == "render-pr-body":
        rounds_markdown = args.rounds_md.read_text(encoding="utf-8") if args.rounds_md.exists() else ""
        print(
            render_pr_body(
                old_commit=args.old_commit,
                new_commit=args.new_commit,
                rounds_markdown=rounds_markdown,
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
