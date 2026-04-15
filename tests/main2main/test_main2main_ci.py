import io
import sys
from argparse import Namespace
from contextlib import redirect_stdout
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts"
sys.path.insert(0, str(SCRIPT_DIR))

import main2main_ci as ci


def make_state(**overrides):
    payload = {
        "pr_number": 188,
        "branch": "main2main_auto_2026-04-03_08-22",
        "head_sha": "1ac49ff7b834177ba43fb7a3044269908bdcbef5",
        "old_commit": "3bfe55a03758d57d4dde7db975842dc281b740a0",
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
        "updated_by": "schedule_main2main_auto.yaml/detect",
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

**Commit range:** `3bfe55a03758d57d4dde7db975842dc281b740a0`...`fa9e68022d29c5396dfbb96d13587b6bc1bdb933`
**Pipeline:** https://github.com/example/repo/actions/runs/123
"""

    metadata = ci.parse_pr_metadata(body)

    assert metadata == ci.PrMetadata(
        old_commit="3bfe55a03758d57d4dde7db975842dc281b740a0",
        new_commit="fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
    )


def test_upsert_pr_phase_section_replaces_existing_section_instead_of_appending():
    body = (
        "## Summary\n\n"
        "Automated adaptation.\n\n"
        "### Phase 2: address E2E-Full CI failures\n"
        "**Pipeline:** old-run\n"
        "**Head commit:** `oldsha`\n\n"
        "```text\nold commit details\n```\n"
    )

    updated = ci.upsert_pr_phase_section(
        body,
        heading="Phase 2: address E2E-Full CI failures",
        content_lines=[
            "**Pipeline:** new-run",
            "**Head commit:** `newsha`",
            "",
            "```text",
            "new commit details",
            "```",
        ],
    )

    assert updated.count("### Phase 2: address E2E-Full CI failures") == 1
    assert "**Pipeline:** new-run" in updated
    assert "**Pipeline:** old-run" not in updated


def test_upsert_pr_phase_section_appends_new_section_when_missing():
    body = "## Summary\n\nAutomated adaptation.\n"

    updated = ci.upsert_pr_phase_section(
        body,
        heading="Phase 3: bisect-guided adaptation",
        content_lines=["**Pipeline:** run-123", "**Head commit:** `abc123`"],
    )

    assert "### Phase 3: bisect-guided adaptation" in updated
    assert updated.count("### Phase 3: bisect-guided adaptation") == 1


def test_parse_registration_comment_extracts_registration_metadata():
    comment = """<!-- main2main-register
pr_number=188
branch=main2main_auto_2026-04-03_08-22
head_sha=1ac49ff7b834177ba43fb7a3044269908bdcbef5
old_commit=3bfe55a03758d57d4dde7db975842dc281b740a0
new_commit=fa9e68022d29c5396dfbb96d13587b6bc1bdb933
phase=2
-->"""

    metadata = ci.parse_registration_comment(comment)

    assert metadata == ci.RegistrationMetadata(
        pr_number=188,
        branch="main2main_auto_2026-04-03_08-22",
        head_sha="1ac49ff7b834177ba43fb7a3044269908bdcbef5",
        old_commit="3bfe55a03758d57d4dde7db975842dc281b740a0",
        new_commit="fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
        phase="2",
    )


def test_select_latest_marker_comment_prefers_highest_comment_id():
    comments = [
        {"id": 101, "body": "hello"},
        {"id": 201, "body": "<!-- main2main-state:v1\n{}\n-->"},
        {"id": 301, "body": '<!-- main2main-state:v1\n{"status":"waiting_e2e"}\n-->'},
    ]

    selected = ci.select_latest_marker_comment(comments, ci.STATE_MARKER)

    assert selected is not None
    assert selected.id == 301
    assert '"status":"waiting_e2e"' in selected.body


def test_load_phase_context_extracts_latest_state_register_and_pr_metadata():
    state = make_state(phase="2", status="fixing", dispatch_token="live-token")
    registration = ci.RegistrationMetadata(
        pr_number=state.pr_number,
        branch=state.branch,
        head_sha=state.head_sha,
        old_commit=state.old_commit,
        new_commit=state.new_commit,
        phase=state.phase,
    )
    pr = {
        "number": state.pr_number,
        "headRefName": state.branch,
        "headRefOid": state.head_sha,
        "body": "ignored",
    }
    comments = [
        {"id": 10, "body": '<!-- main2main-state:v1\n{"status":"waiting_e2e"}\n-->'},
        {"id": 11, "body": ci.render_registration_comment(registration)},
        {"id": 12, "body": ci.render_state_comment(state)},
    ]

    ctx = ci.load_phase_context(
        pr,
        comments,
        expected_phase="2",
        expected_status="fixing",
        dispatch_token="live-token",
    )

    assert ctx.branch == state.branch
    assert ctx.head_sha == state.head_sha
    assert ctx.state_comment_id == 12
    assert ctx.register_comment_id == 11
    assert ctx.state == state
    assert ctx.registration == registration


def test_load_phase_context_accepts_any_of_allowed_statuses():
    state = make_state(phase="3", status="waiting_bisect", dispatch_token="live-token")
    registration = ci.RegistrationMetadata(
        pr_number=state.pr_number,
        branch=state.branch,
        head_sha=state.head_sha,
        old_commit=state.old_commit,
        new_commit=state.new_commit,
        phase=state.phase,
    )
    pr = {
        "number": state.pr_number,
        "headRefName": state.branch,
        "headRefOid": state.head_sha,
        "body": "ignored",
    }
    comments = [
        {"id": 21, "body": ci.render_registration_comment(registration)},
        {"id": 22, "body": ci.render_state_comment(state)},
    ]

    ctx = ci.load_phase_context(
        pr,
        comments,
        expected_phase="3",
        allowed_statuses=["fixing", "waiting_bisect"],
        dispatch_token="live-token",
    )

    assert ctx.state.status == "waiting_bisect"


def test_command_load_phase_context_can_emit_only_state_register_and_ctx_files(tmp_path, monkeypatch):
    state = make_state(phase="2", status="fixing", dispatch_token="live-token")
    registration = ci.RegistrationMetadata(
        pr_number=state.pr_number,
        branch=state.branch,
        head_sha=state.head_sha,
        old_commit=state.old_commit,
        new_commit=state.new_commit,
        phase=state.phase,
    )
    pr = {
        "number": state.pr_number,
        "headRefName": state.branch,
        "headRefOid": state.head_sha,
        "body": "ignored",
        "url": "https://github.com/nv-action/vllm-benchmarks/pull/188",
    }
    comments = [
        {"id": 21, "body": ci.render_registration_comment(registration)},
        {"id": 22, "body": ci.render_state_comment(state)},
    ]

    monkeypatch.setattr(ci, "_gh_json", lambda args: pr)
    monkeypatch.setattr(ci, "_gh_api_json", lambda endpoint, method="GET", payload=None: comments)

    state_json_out = tmp_path / "state.json"
    register_json_out = tmp_path / "register.json"
    ctx_json_out = tmp_path / "ctx.json"

    rc = ci._command_load_phase_context(
        Namespace(
            repo="nv-action/vllm-benchmarks",
            pr_number=str(state.pr_number),
            expected_phase="2",
            expected_status="fixing",
            allowed_statuses=None,
            dispatch_token="live-token",
            pr_json_out=None,
            state_json_out=str(state_json_out),
            registration_json_out=str(register_json_out),
            state_id_out=None,
            register_id_out=None,
            context_json_out=str(ctx_json_out),
        )
    )

    assert rc == 0
    assert ci.json.loads(state_json_out.read_text(encoding="utf-8"))["status"] == "fixing"
    assert ci.json.loads(register_json_out.read_text(encoding="utf-8"))["phase"] == "2"
    ctx = ci.json.loads(ctx_json_out.read_text(encoding="utf-8"))
    assert ctx["branch"] == state.branch
    assert ctx["head_sha"] == state.head_sha
    assert ctx["state_comment_id"] == 22
    assert ctx["register_comment_id"] == 21


def test_resolve_e2e_run_id_from_status_checks_uses_latest_e2e_full_check_run():
    checks = [
        {
            "workflowName": "E2E-Light",
            "detailsUrl": "https://github.com/nv-action/vllm-benchmarks/actions/runs/123/job/1",
        },
        {
            "workflowName": "E2E-Full",
            "detailsUrl": "https://github.com/nv-action/vllm-benchmarks/actions/runs/456/job/2",
        },
        {
            "workflowName": "E2E-Full",
            "detailsUrl": "https://github.com/nv-action/vllm-benchmarks/actions/runs/789/job/3",
        },
    ]

    assert ci.resolve_e2e_run_id_from_status_checks(checks) == "789"


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
    state = make_state(phase="2", status="fixing", head_sha="old-head", e2e_run_id="24000000004")

    updated = ci.apply_fixup_result(state, new_head_sha="new-head")

    assert updated.phase == "3"
    assert updated.status == "waiting_e2e"
    assert updated.head_sha == "new-head"
    assert updated.e2e_run_id == ""


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
    assert updated.status == "manual_review_pending"


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


def test_waiting_bisect_does_not_redispatch_finalize_after_reconcile_already_requested_it():
    state = make_state(
        phase="3",
        status="waiting_bisect",
        bisect_run_id="24120000000",
        last_transition="reconcile->fix_phase3_finalize",
    )

    decision = ci.decide_reconcile_action(
        state,
        bisect_finished=True,
        finalize_missing=False,
    )

    assert decision.action == "wait"


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


def test_registration_consistency_rejects_mismatched_commit_range():
    state = make_state()
    registration = ci.RegistrationMetadata(
        pr_number=state.pr_number,
        branch=state.branch,
        head_sha=state.head_sha,
        old_commit="ffffffffffffffffffffffffffffffffffffffff",
        new_commit=state.new_commit,
        phase=state.phase,
    )

    result = ci.check_registration_consistency(state, registration)

    assert result.ok is False
    assert "commit range" in result.reason


def test_prepare_fix_transition_emits_updated_state_and_registration_artifacts(tmp_path):
    state = make_state(phase="2", status="fixing", head_sha="old-head")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")

    state_json_out = tmp_path / "state-next.json"
    register_json_out = tmp_path / "register-next.json"
    state_comment_out = tmp_path / "state-next.md"
    register_comment_out = tmp_path / "register-next.md"

    rc = ci._command_prepare_fix_transition(
        Namespace(
            state_file=str(state_path),
            result="changes_pushed",
            new_head_sha="new-head",
            fix_run_id="24115639896",
            last_transition="fix_phase2->waiting_e2e",
            updated_by="schedule_main2main_auto.yaml/fix_phase2",
            state_json_out=str(state_json_out),
            register_json_out=str(register_json_out),
            state_comment_out=str(state_comment_out),
            register_comment_out=str(register_comment_out),
            clear_dispatch_token=True,
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    register_payload = ci.json.loads(register_json_out.read_text(encoding="utf-8"))

    assert next_state.phase == "3"
    assert next_state.status == "waiting_e2e"
    assert next_state.head_sha == "new-head"
    assert next_state.fix_run_id == "24115639896"
    assert next_state.dispatch_token == ""
    assert register_payload["head_sha"] == "new-head"
    assert register_payload["phase"] == "3"
    assert "main2main-state:v1" in state_comment_out.read_text(encoding="utf-8")
    assert "main2main-register" in register_comment_out.read_text(encoding="utf-8")


def test_prepare_waiting_bisect_updates_state_and_renders_comment(tmp_path):
    state = make_state(phase="3", status="fixing")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    rc = ci._command_prepare_waiting_bisect(
        Namespace(
            state_file=str(state_path),
            bisect_run_id="24120000000",
            fix_run_id="24115639896",
            last_transition="fix_phase3_prepare->waiting_bisect",
            updated_by="schedule_main2main_auto.yaml/fix_phase3_prepare",
            state_json_out=str(state_json_out),
            state_comment_out=str(state_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    assert next_state.status == "waiting_bisect"
    assert next_state.bisect_run_id == "24120000000"
    assert next_state.fix_run_id == "24115639896"
    assert "main2main-state:v1" in state_comment_out.read_text(encoding="utf-8")


def test_prepare_waiting_bisect_rejects_empty_bisect_run_id(tmp_path):
    state = make_state(phase="3", status="fixing")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    with pytest.raises(SystemExit, match="bisect-run-id"):
        ci._command_prepare_waiting_bisect(
            Namespace(
                state_file=str(state_path),
                bisect_run_id="",
                fix_run_id="24115639896",
                last_transition="fix_phase3_prepare->waiting_bisect",
                updated_by="schedule_main2main_auto.yaml/fix_phase3_prepare",
                state_json_out=str(state_json_out),
                state_comment_out=str(state_comment_out),
            )
        )


def test_prepare_manual_review_pending_updates_state_and_registration(tmp_path):
    state = make_state(phase="3", status="fixing", dispatch_token="live-token")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    register_json_out = tmp_path / "register-next.json"
    state_comment_out = tmp_path / "state-next.md"
    register_comment_out = tmp_path / "register-next.md"

    rc = ci._command_prepare_manual_review_pending(
        Namespace(
            state_file=str(state_path),
            terminal_reason="phase3_no_changes",
            fix_run_id="24120400380",
            last_transition="fix_phase3_finalize->manual_review_pending",
            updated_by="schedule_main2main_auto.yaml/fix_phase3_finalize",
            state_json_out=str(state_json_out),
            register_json_out=str(register_json_out),
            state_comment_out=str(state_comment_out),
            register_comment_out=str(register_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    register_payload = ci.json.loads(register_json_out.read_text(encoding="utf-8"))

    assert next_state.phase == "done"
    assert next_state.status == "manual_review_pending"
    assert next_state.terminal_reason == "phase3_no_changes"
    assert next_state.dispatch_token == "live-token"
    assert next_state.fix_run_id == "24120400380"
    assert next_state.workflow_error_count == 0
    assert register_payload["phase"] == "done"
    assert "main2main-state:v1" in state_comment_out.read_text(encoding="utf-8")
    assert "main2main-register" in register_comment_out.read_text(encoding="utf-8")


def test_prepare_bisect_payload_uses_state_and_ci_analysis_test_cmd(tmp_path):
    state = make_state(phase="3", status="fixing", e2e_run_id="24111111111")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    analysis_path = tmp_path / "ci_analysis.json"
    analysis_path.write_text(
        ci.json.dumps({"test_cmd": "pytest -sv tests/e2e/foo.py"}, ensure_ascii=True), encoding="utf-8"
    )
    payload_out = tmp_path / "bisect-payload.json"

    rc = ci._command_prepare_bisect_payload(
        Namespace(
            state_file=str(state_path),
            ci_analysis_file=str(analysis_path),
            payload_json_out=str(payload_out),
        )
    )

    assert rc == 0
    payload = ci.json.loads(payload_out.read_text(encoding="utf-8"))
    assert payload["e2e_run_id"] == "24111111111"
    assert payload["old_commit"] == state.old_commit
    assert payload["new_commit"] == state.new_commit
    assert payload["test_cmd"] == "pytest -sv tests/e2e/foo.py"


def test_prepare_bisect_payload_falls_back_to_default_test_cmds(tmp_path):
    state = make_state(phase="3", status="fixing", e2e_run_id="24111111111")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    analysis_path = tmp_path / "ci_analysis.json"
    analysis_path.write_text(ci.json.dumps({}, ensure_ascii=True), encoding="utf-8")
    payload_out = tmp_path / "bisect-payload.json"

    rc = ci._command_prepare_bisect_payload(
        Namespace(
            state_file=str(state_path),
            ci_analysis_file=str(analysis_path),
            payload_json_out=str(payload_out),
        )
    )

    assert rc == 0
    payload = ci.json.loads(payload_out.read_text(encoding="utf-8"))
    assert payload["test_cmd"] == ci.DEFAULT_BISECT_TEST_CMD


def test_prepare_bisect_payload_writes_json_to_stdout_when_no_output_file_is_given(tmp_path):
    state = make_state(phase="3", status="fixing", e2e_run_id="24111111111")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    analysis_path = tmp_path / "ci_analysis.json"
    analysis_path.write_text(
        ci.json.dumps({"test_cmd": "pytest -sv tests/e2e/bar.py"}, ensure_ascii=True), encoding="utf-8"
    )

    stdout = io.StringIO()
    with redirect_stdout(stdout):
        rc = ci._command_prepare_bisect_payload(
            Namespace(
                state_file=str(state_path),
                ci_analysis_file=str(analysis_path),
                payload_json_out=None,
            )
        )

    assert rc == 0
    payload = ci.json.loads(stdout.getvalue())
    assert payload["e2e_run_id"] == "24111111111"
    assert payload["old_commit"] == state.old_commit
    assert payload["new_commit"] == state.new_commit
    assert payload["test_cmd"] == "pytest -sv tests/e2e/bar.py"


def test_prepare_fixing_state_updates_fix_run_id_and_comment(tmp_path):
    state = make_state(phase="3", status="waiting_bisect")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    rc = ci._command_prepare_fixing_state(
        Namespace(
            state_file=str(state_path),
            fix_run_id="24120400380",
            last_transition="fix_phase3_finalize->fixing",
            updated_by="schedule_main2main_auto.yaml/fix_phase3_finalize",
            state_json_out=str(state_json_out),
            state_comment_out=str(state_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    assert next_state.status == "fixing"
    assert next_state.fix_run_id == "24120400380"
    assert next_state.last_transition == "fix_phase3_finalize->fixing"
    assert "main2main-state:v1" in state_comment_out.read_text(encoding="utf-8")


def test_select_bisect_run_id_matches_caller_and_dispatch_token():
    runs = [
        {"databaseId": 1, "displayTitle": "bisect standalone"},
        {"databaseId": 2, "displayTitle": "caller-24119446325 token-old"},
        {"databaseId": 3, "displayTitle": "caller-24119446325 token-live-token"},
    ]

    selected = ci.select_bisect_run_id(runs, caller_run_id="24119446325", dispatch_token="live-token")

    assert selected == "3"


def test_prepare_workflow_error_action_retries_before_escalation(tmp_path):
    state = make_state(phase="2", status="fixing", workflow_error_count=0)
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    rc = ci._command_prepare_workflow_error_action(
        Namespace(
            state_file=str(state_path),
            next_dispatch_token="retry-token",
            max_retries=1,
            terminal_reason="workflow_error",
            retry_transition="retry/fix_phase2",
            terminal_transition="error_exhausted/fix_phase2",
            updated_by="schedule_main2main_auto.yaml/fix_phase2_failure",
            state_json_out=str(state_json_out),
            state_comment_out=str(state_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    assert next_state.dispatch_token == "retry-token"
    assert next_state.workflow_error_count == 1
    assert next_state.last_transition == "retry/fix_phase2"


def test_prepare_workflow_error_action_escalates_after_retry_budget(tmp_path):
    state = make_state(phase="2", status="fixing", workflow_error_count=1)
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    rc = ci._command_prepare_workflow_error_action(
        Namespace(
            state_file=str(state_path),
            next_dispatch_token="terminal-token",
            max_retries=1,
            terminal_reason="workflow_error",
            retry_transition="retry/fix_phase2",
            terminal_transition="error_exhausted/fix_phase2",
            updated_by="schedule_main2main_auto.yaml/fix_phase2_failure",
            state_json_out=str(state_json_out),
            state_comment_out=str(state_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    assert next_state.dispatch_token == "terminal-token"
    assert next_state.workflow_error_count == 2
    assert next_state.terminal_reason == "workflow_error"
    assert next_state.last_transition == "error_exhausted/fix_phase2"


def test_prepare_workflow_error_recovery_mints_dispatch_token_and_emits_retry_action(tmp_path):
    state = make_state(phase="2", status="fixing", workflow_error_count=0, dispatch_token="old-token")
    state_path = tmp_path / "state.json"
    state_path.write_text(ci.json.dumps(ci.asdict(state), ensure_ascii=True, indent=2), encoding="utf-8")
    state_json_out = tmp_path / "state-next.json"
    state_comment_out = tmp_path / "state-next.md"

    rc = ci._command_prepare_workflow_error_recovery(
        Namespace(
            state_file=str(state_path),
            max_retries=1,
            terminal_reason="workflow_error",
            retry_transition="retry/fix_phase2",
            terminal_transition="error_exhausted/fix_phase2",
            updated_by="schedule_main2main_auto.yaml/fix_phase2_failure",
            state_json_out=str(state_json_out),
            state_comment_out=str(state_comment_out),
        )
    )

    assert rc == 0
    next_state = ci.Main2MainState(
        **ci._normalize_state_payload(ci.json.loads(state_json_out.read_text(encoding="utf-8")))
    )
    assert next_state.workflow_error_count == 1
    assert next_state.last_transition == "retry/fix_phase2"
    assert next_state.dispatch_token
    assert next_state.dispatch_token != "old-token"
    assert "main2main-state:v1" in state_comment_out.read_text(encoding="utf-8")


def test_reconcile_bootstrap_recovers_missing_register_comment(monkeypatch):
    posts: list[tuple[str, dict]] = []

    def fake_gh_json(args):
        if args[:3] == ["pr", "view", "188"]:
            return {
                "number": 188,
                "headRefName": "main2main_auto_2026-04-03_08-22",
                "headRefOid": "1ac49ff7b834177ba43fb7a3044269908bdcbef5",
                "body": "**Commit range:** `3bfe55a03758d57d4dde7db975842dc281b740a0`...`fa9e68022d29c5396dfbb96d13587b6bc1bdb933`",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "UNSTABLE",
                "url": "https://github.com/nv-action/vllm-benchmarks/pull/188",
                "statusCheckRollup": [],
            }
        if args[:2] == ["run", "list"]:
            return []
        raise AssertionError(f"unexpected gh json args: {args}")

    def fake_gh_api_json(endpoint, *, method="GET", payload=None):
        if method == "GET" and endpoint == "repos/nv-action/vllm-benchmarks/issues/188/comments":
            return []
        if method == "POST" and endpoint == "repos/nv-action/vllm-benchmarks/issues/188/comments":
            posts.append((endpoint, payload))
            return {"id": 1000 + len(posts), "body": payload["body"]}
        raise AssertionError(f"unexpected gh api call: {method} {endpoint}")

    monkeypatch.setattr(ci, "_gh_json", fake_gh_json)
    monkeypatch.setattr(ci, "_gh_api_json", fake_gh_api_json)

    result = ci._reconcile_pr("nv-action/vllm-benchmarks", "188")

    assert result["action"] == "wait"
    assert len(posts) == 2
    assert ci.STATE_MARKER in posts[0][1]["body"] or ci.STATE_MARKER in posts[1][1]["body"]
    assert ci.REGISTER_MARKER in posts[0][1]["body"] or ci.REGISTER_MARKER in posts[1][1]["body"]
