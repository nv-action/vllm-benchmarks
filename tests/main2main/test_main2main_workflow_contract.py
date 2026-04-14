import re
import subprocess
import tempfile
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAIN_WORKFLOW_PATH = WORKFLOWS_DIR / "schedule_main2main_auto.yaml"
BISECT_WORKFLOW_PATH = WORKFLOWS_DIR / "dispatch_main2main_bisect.yaml"


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


def test_auto_workflow_declares_only_target_commit_dispatch_input():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    on_section = workflow_on_section(workflow)

    assert "schedule" in on_section
    assert "workflow_dispatch" in on_section

    dispatch_inputs = on_section["workflow_dispatch"]["inputs"]
    assert list(dispatch_inputs) == ["target_commit", "fix_round_limit", "bisect_round_limit"]


def test_auto_workflow_no_longer_uses_workflow_run_or_legacy_state_machine_inputs():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "workflow_run:" not in text
    for token in [
        "dispatch_token",
        "pr_number",
        "bisect_run_id",
        "schedule_main2main_reconcile.yaml",
        "dispatch_main2main_terminal.yaml",
        "main2main_register_comment",
        "main2main_state_comment",
        "waiting_e2e",
        "waiting_bisect",
        "manual_review_pending",
    ]:
        assert token not in text


def test_auto_workflow_uses_one_long_lived_main_job():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    jobs = workflow["jobs"]

    assert list(jobs) == ["main2main"]
    job = jobs["main2main"]
    assert job["runs-on"] == "linux-aarch64-a3-4"


def test_auto_workflow_runs_fixed_main2main_suite_and_llm_json_summary():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "--suite e2e-main2main" in text
    assert "--continue-on-error" in text
    assert "--format llm-json" in text
    assert "/tmp/main2main-summary.json" in text


def test_auto_workflow_uses_dispatch_inputs_to_gate_fix_and_bisect_rounds():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "inputs.fix_round_limit" in text
    assert "inputs.bisect_round_limit" in text
    assert "fromJSON(inputs.fix_round_limit)" in text
    assert "fromJSON(inputs.bisect_round_limit)" in text


def test_auto_workflow_triggers_bisect_via_gh_workflow_run():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "gh workflow run dispatch_main2main_bisect.yaml" in text
    assert "request_id" in text
    assert "gh run list" in text or "gh run view" in text


def test_auto_workflow_uses_helper_cli_for_bisect_and_finalize_rendering():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "main2main_simplified.py extract-bisect-test-cmd" in text
    assert "main2main_simplified.py build-request-id" in text
    assert "main2main_simplified.py render-pr-body" in text
    assert "main2main_simplified.py render-manual-review-issue" in text


def test_auto_workflow_uses_multiple_visible_steps_and_claude_actions():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    steps = workflow["jobs"]["main2main"]["steps"]
    step_names = [step.get("name") for step in steps]

    for name in [
        "Detect vLLM version change",
        "Stop when no drift is detected",
        "Create working branch",
        "Prepare Claude settings",
        "Claude detect/adapt",
        "Commit detect changes",
        "Initial test",
        "Summarize initial test",
        "Claude fix round 1",
        "Bisect round 1 dispatch",
        "Bisect round 1 wait and download",
        "Claude bisect-fix round 1",
        "Determine final status",
        "Create draft PR",
    ]:
        assert name in step_names

    claude_steps = [
        step for step in steps if str(step.get("uses", "")).startswith("anthropics/claude-code-action/base-action@")
    ]
    assert len(claude_steps) >= 3


def test_auto_workflow_finalizes_with_single_push_and_optional_issue():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "git push" in text
    assert "gh pr create" in text
    assert "--draft" in text
    assert "gh issue create" in text


def test_auto_workflow_run_steps_are_bash_parseable():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    for step in workflow["jobs"]["main2main"]["steps"]:
        if "run" in step:
            bash_syntax_check(step["run"])


def test_bisect_workflow_exposes_only_simplified_dispatch_inputs():
    workflow = load_yaml(BISECT_WORKFLOW_PATH)
    dispatch_inputs = workflow_on_section(workflow)["workflow_dispatch"]["inputs"]

    for key in ["good_commit", "bad_commit", "test_cmd", "request_id"]:
        assert key in dispatch_inputs
    for key in ["caller_type", "caller_run_id", "main2main_pr_number", "main2main_dispatch_token"]:
        assert key not in dispatch_inputs


def test_bisect_workflow_does_not_callback_into_reconcile():
    workflow = load_yaml(BISECT_WORKFLOW_PATH)
    text = read_text(BISECT_WORKFLOW_PATH)

    assert "callback-main2main" not in workflow["jobs"]
    assert "schedule_main2main_reconcile.yaml" not in text
    assert "main2main_dispatch_token" not in text


def test_bisect_workflow_writes_group_named_summary_files_and_request_named_artifacts():
    text = read_text(BISECT_WORKFLOW_PATH)

    assert '--summary-output "/tmp/bisect_summary_${{ matrix.group }}.md"' in text
    assert "bisect-result-${{ inputs.request_id }}-${{ matrix.group }}" in text
    assert "bisect-summary-${{ inputs.request_id }}" in text


def test_bisect_workflow_uses_request_id_not_pr_concurrency():
    text = read_text(BISECT_WORKFLOW_PATH)

    assert "request_id" in text
    assert "main2main_pr_number" not in text
    assert "main2main_dispatch_token" not in text
