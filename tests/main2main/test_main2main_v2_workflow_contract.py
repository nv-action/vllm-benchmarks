from pathlib import Path
import re
import subprocess
import tempfile

import yaml


WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAIN_WORKFLOW_V2_PATH = WORKFLOWS_DIR / "schedule_main2main_auto_v2.yaml"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(read_text(path))


def workflow_on_section(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True) or {}


def bash_syntax_check(script: str) -> None:
    sanitized = re.sub(r"\$\{\{.*?\}\}", "GITHUB_EXPR", script, flags=re.DOTALL)
    with tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False) as handle:
        handle.write(sanitized)
        temp_path = Path(handle.name)
    try:
        result = subprocess.run(
            ["bash", "-n", str(temp_path)],
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
    finally:
        temp_path.unlink(missing_ok=True)


def test_v2_workflow_file_exists():
    assert MAIN_WORKFLOW_V2_PATH.exists()


def test_v2_workflow_declares_only_target_commit_dispatch_input():
    workflow = load_yaml(MAIN_WORKFLOW_V2_PATH)
    on_section = workflow_on_section(workflow)

    assert "schedule" in on_section
    assert "workflow_dispatch" in on_section
    dispatch_inputs = on_section["workflow_dispatch"]["inputs"]
    assert list(dispatch_inputs) == ["target_commit", "fix_round_limit", "bisect_round_limit"]


def test_v2_workflow_uses_one_long_lived_main_job():
    workflow = load_yaml(MAIN_WORKFLOW_V2_PATH)
    jobs = workflow["jobs"]

    assert list(jobs) == ["main2main"]
    assert jobs["main2main"]["runs-on"] == "linux-aarch64-a2-4"


def test_v2_workflow_installs_and_configures_claude_cli():
    text = read_text(MAIN_WORKFLOW_V2_PATH)

    assert "Install Claude Code CLI" in text
    assert "Write Claude settings.json" in text
    assert "settings.json" in text
    assert "claude --version" in text
    assert "claude -p" in text


def test_v2_workflow_uses_cli_loops_not_fixed_round_steps():
    text = read_text(MAIN_WORKFLOW_V2_PATH)

    assert "Run fix loop with Claude CLI" in text
    assert "Run bisect-fix loop with Claude CLI" in text
    assert "while true; do" in text
    assert "Claude fix round 1" not in text
    assert "Claude bisect-fix round 1" not in text
    assert "inputs.fix_round_limit" in text
    assert "inputs.bisect_round_limit" in text
    assert "max_fix_rounds" not in text
    assert "max_bisect_rounds" not in text


def test_v2_workflow_still_uses_bisect_workflow_and_helper_cli():
    text = read_text(MAIN_WORKFLOW_V2_PATH)

    assert "gh workflow run dispatch_main2main_bisect.yaml" in text
    assert "extract-bisect-test-cmd" in text
    assert "build-request-id" in text
    assert "render-pr-body" in text
    assert "render-manual-review-issue" in text
    assert "append-round-commits-markdown" in text


def test_v2_workflow_keeps_suite_and_log_file_contract():
    text = read_text(MAIN_WORKFLOW_V2_PATH)

    assert "--suite e2e-main2main" in text
    assert "--continue-on-error" in text
    assert "/tmp/main2main-test.log" in text
    assert "Use the Main2Main skill log-file entry path." in text
    assert "ci_log_summary.py" not in text


def test_v2_workflow_does_not_use_claude_code_action():
    text = read_text(MAIN_WORKFLOW_V2_PATH)
    assert "anthropics/claude-code-action/base-action@" not in text


def test_v2_workflow_lets_claude_commit_and_workflow_no_longer_commits_directly():
    text = read_text(MAIN_WORKFLOW_V2_PATH)

    assert "Do not commit" not in text
    assert "Create a git commit if and only if you make valid code changes" in text
    assert "git commit -F" not in text


def test_v2_workflow_run_steps_are_bash_parseable():
    workflow = load_yaml(MAIN_WORKFLOW_V2_PATH)
    for step in workflow["jobs"]["main2main"]["steps"]:
        if "run" in step:
            bash_syntax_check(step["run"])
