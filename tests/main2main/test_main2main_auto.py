import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "main2main_auto.py"


def load_module():
    spec = importlib.util.spec_from_file_location("main2main_auto", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def subcommand_choices(parser: argparse.ArgumentParser) -> dict:
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return subparsers_action.choices


def test_cli_description_uses_current_workflow_name():
    module = load_module()

    assert module._build_parser().description == "Helper CLI for main2main auto workflow."


def test_cli_only_exposes_current_workflow_helpers():
    module = load_module()

    commands = set(subcommand_choices(module._build_parser()))

    assert commands == {
        "collect-commit-range",
        "parse-final-summary",
        "render-pr-body",
        "render-manual-review-issue",
    }
    assert "run-claude-phase" not in commands
    assert "run-suite-and-summarize" not in commands
    assert "run-bisect-round" not in commands
    assert "render-prompt" not in commands


def test_render_pr_body_requires_summary_markdown_arg():
    module = load_module()

    with pytest.raises(SystemExit):
        module._build_parser().parse_args(
            [
                "render-pr-body",
                "--old-commit",
                "aaaa",
                "--new-commit",
                "bbbb",
            ]
        )


def test_collect_commit_range_renders_sha_subject_and_body(tmp_path):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    sample = repo / "sample.txt"
    sample.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    sample.write_text("change 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: first change\n\nDetailed explanation for first change."],
        cwd=repo,
        check=True,
    )

    sample.write_text("change 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fix: second change\n\nDetailed explanation for second change."],
        cwd=repo,
        check=True,
    )

    commits = module.collect_commit_range(repo=repo, start_ref=base_ref, end_ref="HEAD")

    assert len(commits) == 2
    assert commits[0]["sha"]
    assert commits[0]["subject"] == "feat: first change"
    assert commits[0]["body"] == "Detailed explanation for first change."
    assert commits[1]["subject"] == "fix: second change"
    assert commits[1]["body"] == "Detailed explanation for second change."


def test_collect_commit_range_handles_commit_without_body(tmp_path):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    sample = repo / "sample.txt"
    sample.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    sample.write_text("change 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "subject only"], cwd=repo, check=True)

    commits = module.collect_commit_range(repo=repo, start_ref=base_ref, end_ref="HEAD")

    assert len(commits) == 1
    assert commits[0]["subject"] == "subject only"
    assert commits[0]["body"] == ""


def test_collect_commit_range_cli_outputs_json(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    sample = repo / "sample.txt"
    sample.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    sample.write_text("change\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "feat: current helper"], cwd=repo, check=True)

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "collect-commit-range",
            "--repo",
            str(repo),
            "--start-ref",
            base_ref,
            "--end-ref",
            "HEAD",
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    commits = json.loads(result.stdout)
    assert len(commits) == 1
    assert commits[0]["subject"] == "feat: current helper"


def test_render_pr_body_places_summary_and_created_commits():
    module = load_module()

    pr_body = module.render_pr_body(
        old_commit="1111111111111111111111111111111111111111",
        new_commit="2222222222222222222222222222222222222222",
        commits_markdown=(
            "- sha: `abc1234511111111111111111111111111111111`\n"
            "  subject: feat: adapt detect path\n"
        ),
        summary_markdown=(
            "Status: completed\n"
            "Steps: 1/1\n"
        ),
    )

    assert pr_body.startswith("Automated adaptation to upstream vLLM main branch changes.")
    assert (
        "Commit range: 1111111111111111111111111111111111111111...2222222222222222222222222222222222222222" in pr_body
    )
    assert "## Created Commits" in pr_body
    assert "## main2main Summary" in pr_body
    assert "subject: feat: adapt detect path" in pr_body
    assert "Status: completed" in pr_body


def test_render_pr_body_cli_uses_summary_and_commits_files(tmp_path):
    summary_path = tmp_path / "summary.md"
    commits_path = tmp_path / "commits.md"
    summary_path.write_text("Status: completed\nSteps: 1/1\n", encoding="utf-8")
    commits_path.write_text("- `abc12345` feat: current helper\n", encoding="utf-8")

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "render-pr-body",
            "--old-commit",
            "aaaa",
            "--new-commit",
            "bbbb",
            "--summary-md",
            str(summary_path),
            "--commits-md",
            str(commits_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Commit range: aaaa...bbbb" in result.stdout
    assert "## Created Commits" in result.stdout
    assert "- `abc12345` feat: current helper" in result.stdout
    assert "Status: completed" in result.stdout


def test_parse_final_summary_markdown_extracts_status_and_partial_stop():
    module = load_module()

    summary = module.parse_final_summary_markdown(
        "\n".join(
            [
                "## Main2Main Summary",
                "",
                "Status: partial",
                "Upstream range: 1111..2222",
                "Reached upstream commit: 1234abcd",
                "Steps: 2/3",
                "CI suite: e2e-main2main",
                "",
                "### Partial Stop",
                "- Stopped at: step-3, upstream range aaaa..bbbb",
                "- Reason: two consecutive rounds with identical error signatures",
                "- Unresolved failures: `TypeError`: missing arg",
                "- Saved patch: /tmp/main2main/steps/step-3/failed.patch",
                "- Saved failure summary: /tmp/main2main/steps/step-3/failed-summary.json",
                "- Repository state: rolled back to last verified vllm-ascend commit cafe",
            ]
        )
    )

    assert summary["status"] == "partial"
    assert summary["upstream_range"] == "1111..2222"
    assert summary["reached_commit"] == "1234abcd"
    assert summary["steps_completed"] == 2
    assert summary["steps_total"] == 3
    assert summary["partial_stop"]["step_id"] == "step-3"
    assert summary["partial_stop"]["reason"] == "two consecutive rounds with identical error signatures"
    assert summary["partial_stop"]["patch_path"] == "/tmp/main2main/steps/step-3/failed.patch"
    assert summary["partial_stop"]["summary_path"] == "/tmp/main2main/steps/step-3/failed-summary.json"


def test_parse_final_summary_cli_outputs_json(tmp_path):
    summary_path = tmp_path / "summary.md"
    summary_path.write_text(
        "Status: completed\n"
        "Upstream range: aaaa..bbbb\n"
        "Reached upstream commit: bbbb\n"
        "Steps: 2/2\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "parse-final-summary",
            "--summary-md",
            str(summary_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    summary = json.loads(result.stdout)
    assert summary["status"] == "completed"
    assert summary["upstream_range"] == "aaaa..bbbb"
    assert summary["reached_commit"] == "bbbb"
    assert summary["steps_completed"] == 2
    assert summary["steps_total"] == 2


def test_render_manual_review_issue_uses_parsed_markdown_summary():
    module = load_module()
    summary_markdown = (
        "## Main2Main Summary\n\n"
        "Status: partial\n"
        "Upstream range: aaaa..bbbb\n"
        "Reached upstream commit: cccc\n"
        "Steps: 1/2\n"
        "CI suite: e2e-main2main\n\n"
        "### Partial Stop\n"
        "- Stopped at: step-2, upstream range bbbb..cccc\n"
        "- Reason: no actionable code bugs\n"
        "- Saved patch: /tmp/main2main/steps/step-2/failed.patch\n"
    )

    issue_body = module.render_manual_review_issue(
        pr_url="https://github.com/nv-action/vllm-benchmarks/pull/999",
        old_commit="aaaa",
        new_commit="bbbb",
        summary=module.parse_final_summary_markdown(summary_markdown),
        summary_markdown=summary_markdown,
    )

    assert "https://github.com/nv-action/vllm-benchmarks/pull/999" in issue_body
    assert "`aaaa`...`bbbb`" in issue_body
    assert "Status: `partial`" in issue_body
    assert "Step: `step-2`" in issue_body
    assert "no actionable code bugs" in issue_body
    assert "## Final Summary" in issue_body
    assert "## Main2Main Summary" in issue_body


def test_render_manual_review_issue_cli_uses_summary_markdown(tmp_path):
    summary_path = tmp_path / "summary.md"
    summary_path.write_text(
        "Status: partial\n"
        "Reached upstream commit: cccc\n"
        "Steps: 1/2\n\n"
        "### Partial Stop\n"
        "- Stopped at: step-2, upstream range bbbb..cccc\n"
        "- Reason: CI did not converge\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "render-manual-review-issue",
            "--pr-url",
            "https://github.com/nv-action/vllm-benchmarks/pull/999",
            "--old-commit",
            "aaaa",
            "--new-commit",
            "bbbb",
            "--summary-md",
            str(summary_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "Status: `partial`" in result.stdout
    assert "Step: `step-2`" in result.stdout
    assert "CI did not converge" in result.stdout
