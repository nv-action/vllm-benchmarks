from pathlib import Path

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAIN_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_auto.yaml"
RECONCILE_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_reconcile.yaml"
TERMINAL_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_terminal.yaml"
LEGACY_MANUAL_REVIEW_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_manual_review.yaml"
BISECT_WORKFLOW_PATH = WORKFLOWS_DIR / "bisect_vllm.yaml"


def read_text(path: Path) -> str:
    return path.read_text()


def test_main_workflow_dispatch_declares_short_phase_contract_inputs():
    text = read_text(MAIN_WORKFLOW_PATH)
    for key in [
        "mode:",
        "target_commit:",
        "pr_number:",
        "dispatch_token:",
        "bisect_run_id:",
    ]:
        assert key in text
    for key in ["\n      branch:", "\n      head_sha:", "\n      run_id:", "\n      phase:", "\n      old_commit:", "\n      new_commit:"]:
        assert key not in text


def test_main_workflow_declares_detect_and_split_fix_modes():
    text = read_text(MAIN_WORKFLOW_PATH)
    for mode in ["detect", "fix_phase2", "fix_phase3_prepare", "fix_phase3_finalize"]:
        assert mode in text


def test_main_workflow_no_longer_uses_workflow_run_trigger():
    text = read_text(MAIN_WORKFLOW_PATH)
    assert "workflow_run:" not in text


def test_main_workflow_publishes_registration_and_live_state_comments():
    text = read_text(MAIN_WORKFLOW_PATH)
    assert "main2main-register" in text
    assert "main2main-state:v1" in text
    assert "main2main_ci.py state-write" in text


def test_reconcile_workflow_exists_with_schedule_and_manual_pr_targeting():
    assert RECONCILE_WORKFLOW_PATH.exists()
    text = read_text(RECONCILE_WORKFLOW_PATH)
    assert "name: Main2Main Reconcile" in text
    assert "schedule:" in text
    assert "workflow_dispatch:" in text
    assert "pr_number:" in text


def test_reconcile_workflow_is_the_waiting_e2e_control_plane():
    text = read_text(RECONCILE_WORKFLOW_PATH)
    assert "waiting_e2e" in text
    assert "fix_phase2" in text
    assert "fix_phase3_prepare" in text
    assert "make_ready" in text
    assert "dispatch_manual_review" in text
    assert "main2main_terminal.yaml" in text


def test_terminal_workflow_renames_manual_review_and_supports_two_actions():
    assert TERMINAL_WORKFLOW_PATH.exists()
    assert not LEGACY_MANUAL_REVIEW_WORKFLOW_PATH.exists()
    text = read_text(TERMINAL_WORKFLOW_PATH)
    assert "name: Main2Main Terminal" in text
    for key in ["action:", "pr_number:", "dispatch_token:", "terminal_reason:"]:
        assert key in text
    assert "make_ready" in text
    assert "manual_review" in text


def test_terminal_workflow_marks_pr_ready_and_creates_issue():
    text = read_text(TERMINAL_WORKFLOW_PATH)
    assert "gh pr ready" in text
    assert "gh issue create" in text
    assert "ci_log_summary.py" in text
    assert "anthropics/claude-code-action/base-action@v1" in text


def test_bisect_workflow_supports_main2main_and_standalone_modes():
    text = read_text(BISECT_WORKFLOW_PATH)
    for key in ["caller_type:", "main2main_pr_number:", "main2main_dispatch_token:"]:
        assert key in text
    assert "standalone" in text
    assert "main2main" in text


def test_bisect_workflow_only_dispatches_finalize_for_main2main_calls():
    text = read_text(BISECT_WORKFLOW_PATH)
    assert "fix_phase3_finalize" in text
    assert "inputs.caller_type == 'main2main'" in text or "inputs.caller_type == \"main2main\"" in text


def test_main2main_bisect_calls_use_per_pr_concurrency():
    text = read_text(BISECT_WORKFLOW_PATH)
    assert "main2main-bisect-pr-{0}" in text or "main2main-bisect-pr-${{ inputs.main2main_pr_number }}" in text
