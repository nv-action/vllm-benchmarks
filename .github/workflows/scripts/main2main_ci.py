from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any

STATE_MARKER = "main2main-state:v1"
REGISTER_MARKER = "main2main-register"

_COMMIT_RANGE_RE = re.compile(
    r"^\*\*Commit range:\*\* `([0-9a-f]{40})`\.\.\.`([0-9a-f]{40})`$",
    re.MULTILINE,
)
_REGISTRATION_COMMENT_RE = re.compile(
    r"<!-- main2main-register\s*"
    r"pr_number=(\d+)\s*"
    r"branch=([^\n]+)\s*"
    r"head_sha=([0-9a-f]{40})\s*"
    r"old_commit=([0-9a-f]{40})\s*"
    r"new_commit=([0-9a-f]{40})\s*"
    r"phase=(2|3|done)\s*"
    r"-->",
    re.MULTILINE,
)
_STATE_COMMENT_RE = re.compile(r"<!-- main2main-state:v1\s*(\{.*?\})\s*-->", re.DOTALL)

_FAILURE_CONCLUSIONS = {
    "action_required",
    "cancelled",
    "failure",
    "stale",
    "startup_failure",
    "timed_out",
}
_CONFLICT_MERGEABLES = {"CONFLICTING"}
_CONFLICT_MERGE_STATE_STATUSES = {"DIRTY", "CONFLICTING"}


@dataclass(frozen=True)
class PrMetadata:
    old_commit: str
    new_commit: str


@dataclass(frozen=True)
class RegistrationMetadata:
    pr_number: int
    branch: str
    head_sha: str
    old_commit: str
    new_commit: str
    phase: str


@dataclass(frozen=True)
class Main2MainState:
    pr_number: int
    branch: str
    head_sha: str
    old_commit: str
    new_commit: str
    phase: str
    status: str
    dispatch_token: str = ""
    e2e_run_id: str = ""
    fix_run_id: str = ""
    bisect_run_id: str = ""
    terminal_reason: str = ""
    last_transition: str = ""
    updated_at: str = ""
    updated_by: str = ""


@dataclass(frozen=True)
class GuardResult:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class ReconcileDecision:
    action: str
    reason: str = ""
    terminal_reason: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class FixupOutcome:
    result: str
    phase: str


def parse_pr_metadata(body: str) -> PrMetadata:
    commit_match = _COMMIT_RANGE_RE.search(body)
    if commit_match is None:
        raise ValueError("PR body is missing main2main metadata")
    return PrMetadata(
        old_commit=commit_match.group(1),
        new_commit=commit_match.group(2),
    )


def parse_registration_comment(body: str) -> RegistrationMetadata:
    match = _REGISTRATION_COMMENT_RE.search(body)
    if match is None:
        raise ValueError("registration comment is missing main2main metadata")
    return RegistrationMetadata(
        pr_number=int(match.group(1)),
        branch=match.group(2),
        head_sha=match.group(3),
        old_commit=match.group(4),
        new_commit=match.group(5),
        phase=match.group(6),
    )


def render_registration_comment(metadata: RegistrationMetadata) -> str:
    return (
        "<!-- main2main-register\n"
        f"pr_number={metadata.pr_number}\n"
        f"branch={metadata.branch}\n"
        f"head_sha={metadata.head_sha}\n"
        f"old_commit={metadata.old_commit}\n"
        f"new_commit={metadata.new_commit}\n"
        f"phase={metadata.phase}\n"
        "-->"
    )


def _normalize_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = {
        "pr_number": int(payload["pr_number"]),
        "branch": payload["branch"],
        "head_sha": payload["head_sha"],
        "old_commit": payload["old_commit"],
        "new_commit": payload["new_commit"],
        "phase": payload["phase"],
        "status": payload["status"],
        "dispatch_token": str(payload.get("dispatch_token", "")),
        "e2e_run_id": str(payload.get("e2e_run_id", "")),
        "fix_run_id": str(payload.get("fix_run_id", "")),
        "bisect_run_id": str(payload.get("bisect_run_id", "")),
        "terminal_reason": str(payload.get("terminal_reason", "")),
        "last_transition": str(payload.get("last_transition", "")),
        "updated_at": str(payload.get("updated_at", "")),
        "updated_by": str(payload.get("updated_by", "")),
    }
    return normalized


def parse_state_comment(body: str) -> Main2MainState:
    match = _STATE_COMMENT_RE.search(body)
    if match is None:
        raise ValueError("state comment is missing main2main metadata")
    payload = json.loads(match.group(1))
    return Main2MainState(**_normalize_state_payload(payload))


def render_state_comment(state: Main2MainState) -> str:
    payload = json.dumps(asdict(state), ensure_ascii=True, indent=2, sort_keys=True)
    return f"<!-- {STATE_MARKER}\n{payload}\n-->"


def init_state_from_registration(
    metadata: RegistrationMetadata,
    *,
    dispatch_token: str,
    updated_at: str = "",
    updated_by: str = "",
) -> Main2MainState:
    return Main2MainState(
        pr_number=metadata.pr_number,
        branch=metadata.branch,
        head_sha=metadata.head_sha,
        old_commit=metadata.old_commit,
        new_commit=metadata.new_commit,
        phase=metadata.phase,
        status="waiting_e2e",
        dispatch_token=dispatch_token,
        last_transition="register->waiting_e2e",
        updated_at=updated_at,
        updated_by=updated_by,
    )


def mint_dispatch_token() -> str:
    return uuid.uuid4().hex


def check_state_guard(
    state: Main2MainState,
    *,
    expected_phase: str | None = None,
    expected_status: str | None = None,
    dispatch_token: str | None = None,
) -> GuardResult:
    if expected_phase and state.phase != expected_phase:
        return GuardResult(False, f"phase mismatch: expected {expected_phase}, got {state.phase}")
    if expected_status and state.status != expected_status:
        return GuardResult(False, f"status mismatch: expected {expected_status}, got {state.status}")
    if dispatch_token is not None and state.dispatch_token != dispatch_token:
        return GuardResult(False, "dispatch token mismatch")
    return GuardResult(True)


def check_pr_consistency(
    state: Main2MainState,
    *,
    branch: str,
    head_sha: str,
    old_commit: str,
    new_commit: str,
) -> GuardResult:
    if state.branch != branch:
        return GuardResult(False, f"branch mismatch: expected {state.branch}, got {branch}")
    if state.head_sha != head_sha:
        return GuardResult(False, f"head_sha mismatch: expected {state.head_sha}, got {head_sha}")
    if state.old_commit != old_commit or state.new_commit != new_commit:
        return GuardResult(False, "commit range mismatch")
    return GuardResult(True)


def _sort_run_key(run: dict[str, Any]) -> tuple[str, int]:
    return (str(run.get("createdAt") or run.get("updatedAt") or ""), int(run.get("databaseId") or 0))


def select_matching_e2e_run(runs: list[dict[str, Any]], *, head_sha: str) -> dict[str, Any] | None:
    matching = [run for run in runs if run.get("headSha") == head_sha]
    if not matching:
        return None
    return max(matching, key=_sort_run_key)


def normalize_conclusion(conclusion: str) -> str:
    if conclusion == "success":
        return "success"
    if conclusion in _FAILURE_CONCLUSIONS:
        return "failure"
    return "skip"


def is_merge_conflict(*, merge_state_status: str | None = None, mergeable: str | None = None) -> bool:
    if mergeable and mergeable.upper() in _CONFLICT_MERGEABLES:
        return True
    if merge_state_status and merge_state_status.upper() in _CONFLICT_MERGE_STATE_STATUSES:
        return True
    return False


def decide_reconcile_action(
    state: Main2MainState,
    *,
    e2e_run: dict[str, Any] | None = None,
    merge_state_status: str | None = None,
    mergeable: str | None = None,
    bisect_finished: bool = False,
    finalize_missing: bool = False,
) -> ReconcileDecision:
    if is_merge_conflict(merge_state_status=merge_state_status, mergeable=mergeable):
        return ReconcileDecision(
            action="dispatch_manual_review",
            terminal_reason="merge_conflict",
            reason="merge conflict blocks further automation",
        )

    if state.status == "waiting_bisect":
        if bisect_finished and finalize_missing:
            return ReconcileDecision(
                action="dispatch_fix_phase3_finalize",
                reason="bisect finished and finalize callback needs recovery",
                run_id=state.bisect_run_id,
            )
        return ReconcileDecision(action="wait", reason="bisect still in progress or finalize already handled")

    if state.status != "waiting_e2e":
        return ReconcileDecision(action="ignore", reason=f"state {state.status} is not handled by reconcile")

    if e2e_run is None:
        return ReconcileDecision(action="wait", reason="matching E2E run not found yet")

    if e2e_run.get("headSha") != state.head_sha:
        return ReconcileDecision(action="wait", reason="stale E2E run does not match current head_sha")

    if e2e_run.get("status") != "completed":
        return ReconcileDecision(action="wait", reason="matching E2E run is still in progress")

    run_id = str(e2e_run.get("databaseId") or "")
    normalized = normalize_conclusion(str(e2e_run.get("conclusion") or ""))
    if normalized == "success":
        return ReconcileDecision(
            action="dispatch_make_ready",
            reason="latest matching E2E run succeeded",
            run_id=run_id,
        )
    if normalized == "failure":
        if state.phase == "2":
            return ReconcileDecision(
                action="dispatch_fix_phase2",
                reason="phase 2 requires another automated fix attempt",
                run_id=run_id,
            )
        if state.phase == "3":
            return ReconcileDecision(
                action="dispatch_fix_phase3_prepare",
                reason="phase 3 requires bisect-guided automated repair",
                run_id=run_id,
            )
        if state.phase == "done":
            return ReconcileDecision(
                action="dispatch_manual_review",
                terminal_reason="done_failure",
                reason="all automated phases are exhausted",
                run_id=run_id,
            )
    return ReconcileDecision(action="ignore", reason=f"unsupported conclusion: {e2e_run.get('conclusion')}")


def apply_fixup_result(state: Main2MainState, *, new_head_sha: str) -> Main2MainState:
    next_phase = "3" if state.phase == "2" else "done"
    return replace(state, head_sha=new_head_sha, phase=next_phase, status="waiting_e2e")


def apply_no_change_fixup_result(state: Main2MainState) -> Main2MainState:
    if state.phase == "2":
        return replace(state, phase="3", status="waiting_e2e")
    return replace(state, phase="done", status="manual_review")


def parse_fixup_job_output(output: str, *, phase: str) -> FixupOutcome:
    if "No changes after phase" in output:
        return FixupOutcome(result="no_changes", phase=phase)
    if "fixes pushed" in output:
        return FixupOutcome(result="changes_pushed", phase=phase)
    raise ValueError("unable to determine fixup outcome from job output")


def _write_json(data: Any) -> None:
    json.dump(data, sys.stdout, ensure_ascii=True, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _load_json(path: str) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _load_text(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


def _write_text(path: str, content: str) -> None:
    Path(path).write_text(content, encoding="utf-8")


def _write_json_file(path: str, payload: Any) -> None:
    _write_text(path, json.dumps(payload, ensure_ascii=True, indent=2))


def _registration_payload_from_state(state: Main2MainState) -> dict[str, Any]:
    return {
        "pr_number": state.pr_number,
        "branch": state.branch,
        "head_sha": state.head_sha,
        "old_commit": state.old_commit,
        "new_commit": state.new_commit,
        "phase": state.phase,
    }


def _run_command(args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        input=input_text,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or f"command failed: {' '.join(args)}")
    return result.stdout


def _gh(args: list[str], *, input_text: str | None = None) -> str:
    return _run_command(["gh", *args], input_text=input_text)


def _gh_json(args: list[str]) -> Any:
    output = _gh(args)
    if not output.strip():
        return None
    return json.loads(output)


def _gh_api_json(endpoint: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> Any:
    args = ["api", endpoint]
    if method != "GET":
        args.extend(["--method", method])
    input_text = None
    if payload is not None:
        args.extend(["--input", "-"])
        input_text = json.dumps(payload)
    output = _gh(args, input_text=input_text)
    if not output.strip():
        return None
    return json.loads(output)


def _command_mint_dispatch_token(_args: argparse.Namespace) -> int:
    sys.stdout.write(f"{mint_dispatch_token()}\n")
    return 0


def _command_state_read(args: argparse.Namespace) -> int:
    state = parse_state_comment(_load_text(args.comment_file))
    _write_json(asdict(state))
    return 0


def _command_state_write(args: argparse.Namespace) -> int:
    payload = _load_json(args.json_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    sys.stdout.write(render_state_comment(state))
    sys.stdout.write("\n")
    return 0


def _command_registration_read(args: argparse.Namespace) -> int:
    metadata = parse_registration_comment(_load_text(args.comment_file))
    _write_json(asdict(metadata))
    return 0


def _command_registration_write(args: argparse.Namespace) -> int:
    payload = _load_json(args.json_file)
    metadata = RegistrationMetadata(**payload)
    sys.stdout.write(render_registration_comment(metadata))
    sys.stdout.write("\n")
    return 0


def _command_state_init_from_register(args: argparse.Namespace) -> int:
    metadata = parse_registration_comment(_load_text(args.comment_file))
    state = init_state_from_registration(
        metadata,
        dispatch_token=args.dispatch_token,
        updated_at=args.updated_at,
        updated_by=args.updated_by,
    )
    _write_json(asdict(state))
    return 0


def _command_guard_check(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    result = check_state_guard(
        state,
        expected_phase=args.expected_phase,
        expected_status=args.expected_status,
        dispatch_token=args.dispatch_token,
    )
    _write_json(asdict(result))
    return 0 if result.ok else 1


def _command_pr_consistency_check(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    result = check_pr_consistency(
        state,
        branch=args.branch,
        head_sha=args.head_sha,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
    )
    _write_json(asdict(result))
    return 0 if result.ok else 1


def _command_reconcile_decision(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    e2e_run = None
    if args.e2e_run_file:
        e2e_run = _load_json(args.e2e_run_file)
    decision = decide_reconcile_action(
        state,
        e2e_run=e2e_run,
        merge_state_status=args.merge_state_status,
        mergeable=args.mergeable,
        bisect_finished=args.bisect_finished,
        finalize_missing=args.finalize_missing,
    )
    _write_json(asdict(decision))
    return 0


def _command_select_e2e_run(args: argparse.Namespace) -> int:
    runs = _load_json(args.runs_file)
    run = select_matching_e2e_run(runs, head_sha=args.head_sha)
    if run is None:
        return 1
    _write_json(run)
    return 0


def _command_apply_fix_result(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    if args.result == "changes_pushed":
        if not args.new_head_sha:
            raise SystemExit("--new-head-sha is required for changes_pushed")
        updated = apply_fixup_result(state, new_head_sha=args.new_head_sha)
    else:
        updated = apply_no_change_fixup_result(state)
    _write_json(asdict(updated))
    return 0


def _command_json_get(args: argparse.Namespace) -> int:
    payload = _load_json(args.json_file)
    value: Any = payload
    for part in args.field.split("."):
        if not isinstance(value, dict) or part not in value:
            raise SystemExit(f"missing field: {args.field}")
        value = value[part]
    sys.stdout.write(str(value))
    sys.stdout.write("\n")
    return 0


def _command_prepare_detect_artifacts(args: argparse.Namespace) -> int:
    register_payload = {
        "pr_number": int(args.pr_number),
        "branch": args.branch,
        "head_sha": args.head_sha,
        "old_commit": args.old_commit,
        "new_commit": args.new_commit,
        "phase": "2",
    }
    state = Main2MainState(
        pr_number=int(args.pr_number),
        branch=args.branch,
        head_sha=args.head_sha,
        old_commit=args.old_commit,
        new_commit=args.new_commit,
        phase="2",
        status="waiting_e2e",
        dispatch_token=args.dispatch_token,
        last_transition="detect->waiting_e2e",
        updated_by=args.updated_by,
    )
    _write_json_file(args.state_json_out, asdict(state))
    _write_json_file(args.register_json_out, register_payload)
    _write_text(args.state_comment_out, render_state_comment(state) + "\n")
    _write_text(
        args.register_comment_out,
        render_registration_comment(RegistrationMetadata(**register_payload)) + "\n",
    )
    return 0


def _command_prepare_fix_transition(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    if args.result == "changes_pushed":
        if not args.new_head_sha:
            raise SystemExit("--new-head-sha is required for changes_pushed")
        next_state = apply_fixup_result(state, new_head_sha=args.new_head_sha)
    else:
        next_state = apply_no_change_fixup_result(state)
    next_state = replace(
        next_state,
        fix_run_id=args.fix_run_id,
        dispatch_token="" if args.clear_dispatch_token else next_state.dispatch_token,
        last_transition=args.last_transition,
        updated_by=args.updated_by,
    )
    register_payload = _registration_payload_from_state(next_state)
    _write_json_file(args.state_json_out, asdict(next_state))
    _write_json_file(args.register_json_out, register_payload)
    _write_text(args.state_comment_out, render_state_comment(next_state) + "\n")
    _write_text(
        args.register_comment_out,
        render_registration_comment(RegistrationMetadata(**register_payload)) + "\n",
    )
    return 0


def _command_prepare_waiting_bisect(args: argparse.Namespace) -> int:
    payload = _load_json(args.state_file)
    state = Main2MainState(**_normalize_state_payload(payload))
    next_state = replace(
        state,
        status="waiting_bisect",
        bisect_run_id=args.bisect_run_id,
        fix_run_id=args.fix_run_id,
        last_transition=args.last_transition,
        updated_by=args.updated_by,
    )
    _write_json_file(args.state_json_out, asdict(next_state))
    _write_text(args.state_comment_out, render_state_comment(next_state) + "\n")
    return 0


def _reconcile_pr(repo: str, pr_number: str) -> dict[str, Any]:
    pr = _gh_json(
        [
            "pr",
            "view",
            pr_number,
            "--repo",
            repo,
            "--json",
            "number,headRefName,headRefOid,body,mergeable,mergeStateStatus,url",
        ]
    )
    comments = _gh_api_json(f"repos/{repo}/issues/{pr_number}/comments")
    register_comment = next((item for item in comments if REGISTER_MARKER in item.get("body", "")), None)
    state_comment = next((item for item in comments if STATE_MARKER in item.get("body", "")), None)

    if state_comment is None:
        token = mint_dispatch_token()
        if register_comment is not None:
            registration = parse_registration_comment(register_comment["body"])
            state = init_state_from_registration(
                registration,
                dispatch_token=token,
                updated_by="main2main_reconcile.yaml/bootstrap",
            )
        else:
            metadata = parse_pr_metadata(pr.get("body") or "")
            state = Main2MainState(
                pr_number=int(pr["number"]),
                branch=pr["headRefName"],
                head_sha=pr["headRefOid"],
                old_commit=metadata.old_commit,
                new_commit=metadata.new_commit,
                phase="2",
                status="waiting_e2e",
                dispatch_token=token,
                last_transition="reconcile/recover->waiting_e2e",
                updated_by="main2main_reconcile.yaml/recover",
            )
        created = _gh_api_json(
            f"repos/{repo}/issues/{pr_number}/comments",
            method="POST",
            payload={"body": render_state_comment(state)},
        )
        state_comment_id = int(created["id"])
    else:
        state = parse_state_comment(state_comment["body"])
        state_comment_id = int(state_comment["id"])

    metadata = parse_pr_metadata(pr.get("body") or "")
    consistency = check_pr_consistency(
        state,
        branch=pr["headRefName"],
        head_sha=pr["headRefOid"],
        old_commit=metadata.old_commit,
        new_commit=metadata.new_commit,
    )
    if not consistency.ok:
        return {"action": "skip", "reason": consistency.reason}

    if state.status == "waiting_e2e":
        runs = _gh_json(
            [
                "run",
                "list",
                "--repo",
                repo,
                "--workflow",
                "pr_test_full.yaml",
                "-L",
                "50",
                "--json",
                "databaseId,headSha,status,conclusion,createdAt,url",
            ]
        )
        matched_run = select_matching_e2e_run(runs or [], head_sha=state.head_sha)
        decision = decide_reconcile_action(
            state,
            e2e_run=matched_run,
            merge_state_status=pr.get("mergeStateStatus"),
            mergeable=pr.get("mergeable"),
        )
    elif state.status == "waiting_bisect":
        bisect_finished = False
        if state.bisect_run_id:
            try:
                bisect_meta = _gh_json(
                    ["run", "view", state.bisect_run_id, "--repo", repo, "--json", "status,conclusion"]
                )
            except RuntimeError:
                bisect_meta = {}
            bisect_finished = (bisect_meta or {}).get("status") == "completed"
        decision = decide_reconcile_action(
            state,
            merge_state_status=pr.get("mergeStateStatus"),
            mergeable=pr.get("mergeable"),
            bisect_finished=bisect_finished,
            finalize_missing=bisect_finished,
        )
    else:
        decision = ReconcileDecision(action="ignore", reason=f"state {state.status} is not handled by reconcile")

    if decision.action in {"wait", "ignore"}:
        return {"action": decision.action, "reason": decision.reason}

    token = mint_dispatch_token()
    next_state = replace(state, dispatch_token=token, updated_by="main2main_reconcile.yaml")
    if decision.run_id:
        next_state = replace(next_state, e2e_run_id=decision.run_id)
    if decision.action == "dispatch_fix_phase2":
        next_state = replace(next_state, status="fixing", phase="2", last_transition="reconcile->fix_phase2")
    elif decision.action == "dispatch_fix_phase3_prepare":
        next_state = replace(next_state, status="fixing", phase="3", last_transition="reconcile->fix_phase3_prepare")
    elif decision.action == "dispatch_manual_review":
        next_state = replace(
            next_state,
            terminal_reason=decision.terminal_reason,
            last_transition="reconcile->manual_review",
        )
    elif decision.action == "dispatch_make_ready":
        next_state = replace(next_state, last_transition="reconcile->make_ready")
    elif decision.action == "dispatch_fix_phase3_finalize":
        next_state = replace(next_state, last_transition="reconcile->fix_phase3_finalize")

    _gh_api_json(
        f"repos/{repo}/issues/comments/{state_comment_id}",
        method="PATCH",
        payload={"body": render_state_comment(next_state)},
    )

    if decision.action == "dispatch_fix_phase2":
        _gh(
            [
                "workflow",
                "run",
                "main2main_auto.yaml",
                "--repo",
                repo,
                "-f",
                "mode=fix_phase2",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"dispatch_token={token}",
            ]
        )
    elif decision.action == "dispatch_fix_phase3_prepare":
        _gh(
            [
                "workflow",
                "run",
                "main2main_auto.yaml",
                "--repo",
                repo,
                "-f",
                "mode=fix_phase3_prepare",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"dispatch_token={token}",
            ]
        )
    elif decision.action == "dispatch_fix_phase3_finalize":
        _gh(
            [
                "workflow",
                "run",
                "main2main_auto.yaml",
                "--repo",
                repo,
                "-f",
                "mode=fix_phase3_finalize",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"dispatch_token={token}",
                "-f",
                f"bisect_run_id={next_state.bisect_run_id}",
            ]
        )
    elif decision.action == "dispatch_make_ready":
        _gh(
            [
                "workflow",
                "run",
                "main2main_terminal.yaml",
                "--repo",
                repo,
                "-f",
                "action=make_ready",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"dispatch_token={token}",
            ]
        )
    elif decision.action == "dispatch_manual_review":
        _gh(
            [
                "workflow",
                "run",
                "main2main_terminal.yaml",
                "--repo",
                repo,
                "-f",
                "action=manual_review",
                "-f",
                f"pr_number={pr_number}",
                "-f",
                f"dispatch_token={token}",
                "-f",
                f"terminal_reason={decision.terminal_reason}",
            ]
        )

    return {"action": decision.action, "reason": decision.reason, "run_id": decision.run_id}


def _command_reconcile_pr(args: argparse.Namespace) -> int:
    result = _reconcile_pr(args.repo, args.pr_number)
    _write_json(result)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Workflow-native main2main CI helper")
    subparsers = parser.add_subparsers(dest="command", required=True)

    mint = subparsers.add_parser("mint-dispatch-token")
    mint.set_defaults(func=_command_mint_dispatch_token)

    state_read = subparsers.add_parser("state-read")
    state_read.add_argument("--comment-file", required=True)
    state_read.set_defaults(func=_command_state_read)

    state_write = subparsers.add_parser("state-write")
    state_write.add_argument("--json-file", required=True)
    state_write.set_defaults(func=_command_state_write)

    register_read = subparsers.add_parser("registration-read")
    register_read.add_argument("--comment-file", required=True)
    register_read.set_defaults(func=_command_registration_read)

    register_write = subparsers.add_parser("registration-write")
    register_write.add_argument("--json-file", required=True)
    register_write.set_defaults(func=_command_registration_write)

    init_state = subparsers.add_parser("state-init-from-register")
    init_state.add_argument("--comment-file", required=True)
    init_state.add_argument("--dispatch-token", required=True)
    init_state.add_argument("--updated-at", default="")
    init_state.add_argument("--updated-by", default="")
    init_state.set_defaults(func=_command_state_init_from_register)

    guard = subparsers.add_parser("guard-check")
    guard.add_argument("--state-file", required=True)
    guard.add_argument("--expected-phase", default=None)
    guard.add_argument("--expected-status", default=None)
    guard.add_argument("--dispatch-token", default=None)
    guard.set_defaults(func=_command_guard_check)

    consistency = subparsers.add_parser("pr-consistency-check")
    consistency.add_argument("--state-file", required=True)
    consistency.add_argument("--branch", required=True)
    consistency.add_argument("--head-sha", required=True)
    consistency.add_argument("--old-commit", required=True)
    consistency.add_argument("--new-commit", required=True)
    consistency.set_defaults(func=_command_pr_consistency_check)

    reconcile = subparsers.add_parser("reconcile-decision")
    reconcile.add_argument("--state-file", required=True)
    reconcile.add_argument("--e2e-run-file", default="")
    reconcile.add_argument("--merge-state-status", default=None)
    reconcile.add_argument("--mergeable", default=None)
    reconcile.add_argument("--bisect-finished", action="store_true")
    reconcile.add_argument("--finalize-missing", action="store_true")
    reconcile.set_defaults(func=_command_reconcile_decision)

    reconcile_pr = subparsers.add_parser("reconcile-pr")
    reconcile_pr.add_argument("--repo", required=True)
    reconcile_pr.add_argument("--pr-number", required=True)
    reconcile_pr.set_defaults(func=_command_reconcile_pr)

    select_run = subparsers.add_parser("select-e2e-run")
    select_run.add_argument("--runs-file", required=True)
    select_run.add_argument("--head-sha", required=True)
    select_run.set_defaults(func=_command_select_e2e_run)

    apply_fix = subparsers.add_parser("apply-fix-result")
    apply_fix.add_argument("--state-file", required=True)
    apply_fix.add_argument("--result", required=True, choices=["changes_pushed", "no_changes"])
    apply_fix.add_argument("--new-head-sha", default="")
    apply_fix.set_defaults(func=_command_apply_fix_result)

    json_get = subparsers.add_parser("json-get")
    json_get.add_argument("--json-file", required=True)
    json_get.add_argument("--field", required=True)
    json_get.set_defaults(func=_command_json_get)

    detect_artifacts = subparsers.add_parser("prepare-detect-artifacts")
    detect_artifacts.add_argument("--pr-number", required=True)
    detect_artifacts.add_argument("--branch", required=True)
    detect_artifacts.add_argument("--head-sha", required=True)
    detect_artifacts.add_argument("--old-commit", required=True)
    detect_artifacts.add_argument("--new-commit", required=True)
    detect_artifacts.add_argument("--dispatch-token", required=True)
    detect_artifacts.add_argument("--updated-by", default="main2main_auto.yaml/detect")
    detect_artifacts.add_argument("--state-json-out", required=True)
    detect_artifacts.add_argument("--register-json-out", required=True)
    detect_artifacts.add_argument("--state-comment-out", required=True)
    detect_artifacts.add_argument("--register-comment-out", required=True)
    detect_artifacts.set_defaults(func=_command_prepare_detect_artifacts)

    fix_transition = subparsers.add_parser("prepare-fix-transition")
    fix_transition.add_argument("--state-file", required=True)
    fix_transition.add_argument("--result", required=True, choices=["changes_pushed", "no_changes"])
    fix_transition.add_argument("--new-head-sha", default="")
    fix_transition.add_argument("--fix-run-id", required=True)
    fix_transition.add_argument("--last-transition", required=True)
    fix_transition.add_argument("--updated-by", required=True)
    fix_transition.add_argument("--state-json-out", required=True)
    fix_transition.add_argument("--register-json-out", required=True)
    fix_transition.add_argument("--state-comment-out", required=True)
    fix_transition.add_argument("--register-comment-out", required=True)
    fix_transition.add_argument("--clear-dispatch-token", action="store_true")
    fix_transition.set_defaults(func=_command_prepare_fix_transition)

    waiting_bisect = subparsers.add_parser("prepare-waiting-bisect")
    waiting_bisect.add_argument("--state-file", required=True)
    waiting_bisect.add_argument("--bisect-run-id", required=True)
    waiting_bisect.add_argument("--fix-run-id", required=True)
    waiting_bisect.add_argument("--last-transition", required=True)
    waiting_bisect.add_argument("--updated-by", required=True)
    waiting_bisect.add_argument("--state-json-out", required=True)
    waiting_bisect.add_argument("--state-comment-out", required=True)
    waiting_bisect.set_defaults(func=_command_prepare_waiting_bisect)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
