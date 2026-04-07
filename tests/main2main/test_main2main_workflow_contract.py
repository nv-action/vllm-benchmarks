from pathlib import Path

MAIN_WORKFLOW_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "main2main_auto.yaml"
MANUAL_REVIEW_WORKFLOW_PATH = (
    Path(__file__).resolve().parents[2] / ".github" / "workflows" / "main2main_manual_review.yaml"
)


def read_main_workflow() -> str:
    return MAIN_WORKFLOW_PATH.read_text()


def read_manual_review_workflow() -> str:
    return MANUAL_REVIEW_WORKFLOW_PATH.read_text()


def test_workflow_dispatch_declares_fixup_contract_inputs():
    text = read_main_workflow()
    for key in [
        "mode:",
        "pr_number:",
        "branch:",
        "head_sha:",
        "run_id:",
        "phase:",
        "old_commit:",
        "new_commit:",
        "dispatch_token:",
    ]:
        assert key in text


def test_workflow_no_longer_uses_workflow_run_trigger():
    text = read_main_workflow()
    assert "workflow_run:" not in text


def test_fixup_job_uses_explicit_fixup_mode_gate():
    text = read_main_workflow()
    assert "if: github.event_name == 'workflow_dispatch' && github.event.inputs.mode == 'fixup'" in text
    assert "Find main2main PR for this run" not in text


def test_main_workflow_dispatches_separate_manual_review_workflow():
    text = read_manual_review_workflow()
    assert "name: Main2Main Manual Review" in text


def test_manual_review_workflow_declares_small_dispatch_contract():
    text = read_manual_review_workflow()
    for key in [
        "pr_number:",
        "dispatch_token:",
        "terminal_reason:",
        "e2e_run_id:",
        "fixup_run_id:",
    ]:
        assert key in text
    assert "manual_review_body:" not in text
    assert "manual_review_title:" not in text


def test_manual_review_workflow_generates_issue_body_via_claude_and_creates_issue():
    text = read_manual_review_workflow()
    assert "ci_log_summary.py" in text
    assert "anthropics/claude-code-action/base-action@v1" in text
    assert 'gh issue create \\' in text
    assert "manual_review_issue.md" in text


def test_fixup_no_longer_tracks_phase_via_labels():
    text = read_main_workflow()
    assert "main2main-phase2" not in text
    assert "main2main-phase3" not in text
    assert '--add-label "${PHASE_LABEL}"' not in text


def test_phase1_publishes_registration_comment_for_orchestrator():
    text = read_main_workflow()
    assert "main2main-register" in text
    assert 'gh pr comment "${{ steps.pr.outputs.number }}"' in text
    assert "head_sha=$(git rev-parse HEAD)" in text
    assert "phase=2" in text


def test_fixup_run_name_includes_dispatch_token():
    text = read_main_workflow()
    assert "run-name:" in text
    assert "github.event.inputs.dispatch_token" in text


def test_fixup_updates_registration_comment_after_pushing_changes():
    text = read_main_workflow()
    assert "Update registration comment for orchestrator" in text
    assert 'gh api "repos/${{ env.UPSTREAM_REPO }}/issues/${{ steps.ctx.outputs.pr_number }}/comments"' in text
    assert "phase=${NEXT_PHASE}" in text
    assert "head_sha=$(git rev-parse HEAD)" in text
