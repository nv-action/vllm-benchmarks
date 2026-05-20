import argparse
import importlib.util
import json
import subprocess
from pathlib import Path

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
        "print-claude-stream",
        "render-manual-review-issue",
    }
    assert "run-claude-phase" not in commands
    assert "run-suite-and-summarize" not in commands
    assert "run-bisect-round" not in commands
    assert "render-prompt" not in commands


def test_print_claude_stream_cli_renders_readable_conversation(tmp_path):
    stream_path = tmp_path / "claude.stream.jsonl"
    stream_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "system", "subtype": "init", "session_id": "session-1"}),
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {
                            "content": [
                                {"type": "text", "text": "I will inspect the repository."},
                                {"type": "tool_use", "name": "Bash", "input": {"command": "git status --short"}},
                            ]
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "message": {
                            "content": [
                                {"type": "tool_result", "content": " M .github/workflows/schedule_main2main_auto.yaml"}
                            ]
                        },
                    }
                ),
                json.dumps({"type": "result", "subtype": "success", "duration_ms": 1234}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "print-claude-stream",
            "--input",
            str(stream_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert "[system] init session_id=session-1" in result.stdout
    assert "[assistant]" in result.stdout
    assert "I will inspect the repository." in result.stdout
    assert "[tool_use] Bash" in result.stdout
    assert "git status --short" in result.stdout
    assert "[tool_result]" in result.stdout
    assert "M .github/workflows/schedule_main2main_auto.yaml" in result.stdout
    assert "[result] success duration_ms=1234" in result.stdout


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
                "- Unresolved failures: missing arg",
                "- Saved patch: /tmp/main2main/steps/step-3/failed.patch",
                "- Saved failure summary: /tmp/main2main/steps/step-3/failed-summary.json",
                "- Repository state: rolled back to last verified vllm-ascend commit cafe",
                "",
                "### Follow-up",
                "- Reason: this belongs to a different section",
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
    assert summary["partial_stop"]["unresolved_failures"] == "missing arg"
    assert summary["partial_stop"]["patch_path"] == "/tmp/main2main/steps/step-3/failed.patch"
    assert summary["partial_stop"]["summary_path"] == "/tmp/main2main/steps/step-3/failed-summary.json"
    assert (
        summary["partial_stop"]["repository_state"]
        == "rolled back to last verified vllm-ascend commit cafe"
    )


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
