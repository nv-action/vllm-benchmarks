import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import main2main_ci as ci


def make_state(**overrides):
    payload = {
        "pr_number": 188,
        "branch": "main2main_auto_2026-04-03_08-22",
        "head_sha": "1ac49ff7b834177ba43fb7a3044269908bdcbef5",
        "old_commit": "35141a7eeda941a60ad5a4956670c60fd5a77029",
        "new_commit": "fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
        "phase": "2",
        "status": "waiting_e2e",
        "dispatch_token": "dispatch-188",
        "e2e_run_id": "",
        "fix_run_id": "",
        "bisect_run_id": "",
        "terminal_reason": "",
        "last_transition": "detect->waiting_e2e",
        "updated_at": "2026-04-07T12:00:00Z",
        "updated_by": "main2main_auto.yaml/detect",
    }
    payload.update(overrides)
    return ci.Main2MainState(**payload)


def test_parse_main2main_state_comment_round_trip():
    state = make_state(phase="3", status="waiting_bisect", bisect_run_id="24000000002")

    comment = ci.render_state_comment(state)
    parsed = ci.parse_state_comment(comment)

    assert parsed == state


def test_parse_pr_metadata_extracts_commit_range_only():
    body = """
## Summary

**Commit range:** `35141a7eeda941a60ad5a4956670c60fd5a77029`...`fa9e68022d29c5396dfbb96d13587b6bc1bdb933`
**Pipeline:** https://github.com/example/repo/actions/runs/123
"""

    metadata = ci.parse_pr_metadata(body)

    assert metadata == ci.PrMetadata(
        old_commit="35141a7eeda941a60ad5a4956670c60fd5a77029",
        new_commit="fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
    )


def test_parse_registration_comment_extracts_registration_metadata():
    comment = """<!-- main2main-register
pr_number=188
branch=main2main_auto_2026-04-03_08-22
head_sha=1ac49ff7b834177ba43fb7a3044269908bdcbef5
old_commit=35141a7eeda941a60ad5a4956670c60fd5a77029
new_commit=fa9e68022d29c5396dfbb96d13587b6bc1bdb933
phase=2
-->"""

    metadata = ci.parse_registration_comment(comment)

    assert metadata == ci.RegistrationMetadata(
        pr_number=188,
        branch="main2main_auto_2026-04-03_08-22",
        head_sha="1ac49ff7b834177ba43fb7a3044269908bdcbef5",
        old_commit="35141a7eeda941a60ad5a4956670c60fd5a77029",
        new_commit="fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
        phase="2",
    )


def test_guard_check_rejects_mismatched_dispatch_token():
    state = make_state(dispatch_token="expected-token", phase="2", status="fixing")

    result = ci.check_state_guard(
        state,
        expected_phase="2",
        expected_status="fixing",
        dispatch_token="wrong-token",
    )

    assert result.ok is False
    assert "dispatch token" in result.reason.lower()


def test_guard_check_rejects_empty_dispatch_token_when_state_has_live_token():
    state = make_state(dispatch_token="live-token", phase="2", status="fixing")

    result = ci.check_state_guard(
        state,
        expected_phase="2",
        expected_status="fixing",
        dispatch_token="",
    )

    assert result.ok is False
    assert "dispatch token" in result.reason.lower()


def test_pr_consistency_rejects_head_sha_mismatch():
    state = make_state()

    result = ci.check_pr_consistency(
        state,
        branch=state.branch,
        head_sha="ffffffffffffffffffffffffffffffffffffffff",
        old_commit=state.old_commit,
        new_commit=state.new_commit,
    )

    assert result.ok is False
    assert "head_sha" in result.reason


def test_phase2_no_changes_moves_to_phase3_waiting_e2e_without_new_head():
    state = make_state(phase="2", status="fixing")

    updated = ci.apply_no_change_fixup_result(state)

    assert updated.phase == "3"
    assert updated.status == "waiting_e2e"
    assert updated.head_sha == state.head_sha


def test_reconcile_ignores_e2e_runs_for_old_heads():
    state = make_state(phase="2", status="waiting_e2e", head_sha="new-head")
    runs = [
        {
            "databaseId": 11,
            "headSha": "old-head",
            "status": "completed",
            "conclusion": "failure",
        }
    ]

    match = ci.select_matching_e2e_run(runs, head_sha=state.head_sha)
    decision = ci.decide_reconcile_action(state, e2e_run=match)

    assert match is None
    assert decision.action == "wait"


def test_phase2_changes_move_to_phase3_waiting_e2e_with_new_head():
    state = make_state(phase="2", status="fixing", head_sha="old-head")

    updated = ci.apply_fixup_result(state, new_head_sha="new-head")

    assert updated.phase == "3"
    assert updated.status == "waiting_e2e"
    assert updated.head_sha == "new-head"


def test_phase3_changes_move_to_done_waiting_e2e():
    state = make_state(phase="3", status="fixing", head_sha="old-head")

    updated = ci.apply_fixup_result(state, new_head_sha="new-head")

    assert updated.phase == "done"
    assert updated.status == "waiting_e2e"
    assert updated.head_sha == "new-head"


def test_phase3_no_changes_move_to_manual_review():
    state = make_state(phase="3", status="fixing")

    updated = ci.apply_no_change_fixup_result(state)

    assert updated.phase == "done"
    assert updated.status == "manual_review"


def test_parse_fixup_job_output_detects_changes_pushed():
    output = """
✓ fixup in 5m28s (ID 66567079828)
ANNOTATIONS
- Phase 2 fixes pushed. External orchestration should trigger the next E2E-Full cycle.
"""

    outcome = ci.parse_fixup_job_output(output, phase="2")

    assert outcome == ci.FixupOutcome(result="changes_pushed", phase="2")


def test_parse_fixup_job_output_detects_no_changes_for_phase3():
    output = """
✓ fixup in 21m18s (ID 66570006601)
ANNOTATIONS
! No changes after phase 3 fix attempt.
"""

    outcome = ci.parse_fixup_job_output(output, phase="3")

    assert outcome == ci.FixupOutcome(result="no_changes", phase="3")


def test_done_failure_transitions_to_manual_review():
    state = make_state(phase="done", status="waiting_e2e")
    run = {
        "databaseId": 24000000003,
        "headSha": state.head_sha,
        "status": "completed",
        "conclusion": "failure",
    }

    decision = ci.decide_reconcile_action(state, e2e_run=run)

    assert decision.action == "dispatch_manual_review"
    assert decision.terminal_reason == "done_failure"


def test_waiting_e2e_success_dispatches_make_ready():
    state = make_state(phase="3", status="waiting_e2e")
    run = {
        "databaseId": 24000000004,
        "headSha": state.head_sha,
        "status": "completed",
        "conclusion": "success",
    }

    decision = ci.decide_reconcile_action(state, e2e_run=run)

    assert decision.action == "dispatch_make_ready"


def test_merge_conflict_goes_terminal_before_e2e_progression():
    state = make_state(phase="2", status="waiting_e2e")
    run = {
        "databaseId": 24000000005,
        "headSha": state.head_sha,
        "status": "completed",
        "conclusion": "failure",
    }

    decision = ci.decide_reconcile_action(state, e2e_run=run, merge_state_status="DIRTY")

    assert decision.action == "dispatch_manual_review"
    assert decision.terminal_reason == "merge_conflict"
