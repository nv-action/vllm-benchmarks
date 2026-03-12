import json
from pathlib import Path
import sys
import tempfile
import subprocess
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import main2main_orchestrator as orchestrator

from main2main_orchestrator import (
    GitHubCliAdapter,
    FixupOutcome,
    Main2MainState,
    Main2MainStateStore,
    OrchestratorService,
    PrMetadata,
    RegistrationMetadata,
    _main,
    apply_fixup_result,
    apply_no_change_fixup_result,
    decide_next_action,
    extract_e2e_failure_analysis,
    parse_fixup_job_output,
    parse_pr_metadata,
    parse_registration_comment,
    run_loop,
    summarize_manual_review_issue,
)


def test_parse_pr_metadata_extracts_commit_range_only():
    body = """
## Summary

**Commit range:** `4034c3d32e30d01639459edd3ab486f56993876d`...`4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366`
**Pipeline:** https://github.com/example/repo/actions/runs/123
"""

    metadata = parse_pr_metadata(body)

    assert metadata == PrMetadata(
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
    )


def test_parse_fixup_job_output_detects_changes_pushed():
    output = """
✓ fixup in 5m28s (ID 66567079828)
ANNOTATIONS
- Phase 2 fixes pushed. External orchestration should trigger the next E2E-Full cycle.
"""

    outcome = parse_fixup_job_output(output, phase="2")

    assert outcome == FixupOutcome(result="changes_pushed", phase="2")


def test_parse_fixup_job_output_detects_no_changes_for_phase3():
    output = """
✓ fixup in 21m18s (ID 66570006601)
ANNOTATIONS
! No changes after phase 3 fix attempt.
"""

    outcome = parse_fixup_job_output(output, phase="3")

    assert outcome == FixupOutcome(result="no_changes", phase="3")


def test_parse_registration_comment_extracts_registration_metadata():
    comment = """<!-- main2main-register
pr_number=149
branch=main2main_auto_2026-03-11_02-02
head_sha=0ac6428474c21eed75ceacac5b7fc04c58512a95
old_commit=4034c3d32e30d01639459edd3ab486f56993876d
new_commit=81939e7733642f583d1731e5c9ef69dcd457b5e5
phase=2
-->"""

    metadata = parse_registration_comment(comment)

    assert metadata == RegistrationMetadata(
        pr_number=149,
        branch="main2main_auto_2026-03-11_02-02",
        head_sha="0ac6428474c21eed75ceacac5b7fc04c58512a95",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
        phase="2",
    )


def test_extract_e2e_failure_analysis_invokes_script_with_repo_and_run_id(monkeypatch):
    calls = []

    class Completed:
        stdout = '{"run_id": 123, "code_bugs": []}'

    def fake_run(args, capture_output, text, check):
        calls.append(args)
        return Completed()

    monkeypatch.setattr(subprocess, "run", fake_run)

    result = extract_e2e_failure_analysis(
        repo="nv-action/vllm-benchmarks",
        run_id="123",
    )

    assert result == {"run_id": 123, "code_bugs": []}
    assert "--repo" in calls[0]
    assert "nv-action/vllm-benchmarks" in calls[0]
    assert "--run-id" in calls[0]
    assert "123" in calls[0]
    assert "--llm-output" in calls[0]


def test_summarize_manual_review_issue_uses_claude_gateway(monkeypatch):
    requests = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "content": [
                        {
                            "type": "text",
                            "text": "Summary line\\n- cause\\n- next step",
                        }
                    ]
                }
            ).encode("utf-8")

    def fake_urlopen(request):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.example")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "secret-token")
    monkeypatch.setattr(orchestrator.urllib.request, "urlopen", fake_urlopen)

    summary = summarize_manual_review_issue(
        analysis={"run_id": 123, "code_bugs": [{"error_type": "TypeError", "error_message": "bad"}]},
        state=Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=154,
            branch="main2main_auto_branch",
            head_sha="abc",
            old_commit="0" * 40,
            new_commit="1" * 40,
            phase="done",
            status="waiting_e2e",
        ),
        terminal_reason="done_failure",
        e2e_run_url="https://example/e2e/123",
        e2e_run_id="123",
        fixup_run_id="456",
    )

    assert "Summary line" in summary
    assert len(requests) == 1
    request = requests[0]
    assert request.full_url == "https://gateway.example/v1/messages"
    assert request.headers["Authorization"] == "Bearer secret-token"
    payload = json.loads(request.data.decode("utf-8"))
    assert payload["model"] == "claude-sonnet-4-5"
    assert "done_failure" in payload["messages"][0]["content"]


def test_decide_next_action_marks_ready_on_success():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="2",
        status="waiting_e2e",
    )

    decision = decide_next_action(state, head_sha="abc123", conclusion="success")

    assert decision.action == "mark_ready"
    assert decision.phase == "2"


def test_decide_next_action_dispatches_fixup_for_failure_like_results():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="2",
        status="waiting_e2e",
    )

    for conclusion in ("failure", "cancelled", "timed_out"):
        decision = decide_next_action(state, head_sha="abc123", conclusion=conclusion)
        assert decision.action == "dispatch_fixup"
        assert decision.phase == "2"


def test_decide_next_action_creates_manual_review_after_done_phase_failure():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="done",
        status="waiting_e2e",
    )

    decision = decide_next_action(state, head_sha="abc123", conclusion="failure")

    assert decision.action == "create_manual_review"
    assert decision.phase == "done"


def test_decide_next_action_ignores_stale_head_sha():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="newsha",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="3",
        status="waiting_e2e",
    )

    decision = decide_next_action(state, head_sha="oldsha", conclusion="failure")

    assert decision.action == "ignore"
    assert "stale" in decision.reason


def test_apply_fixup_result_advances_phase_and_head_sha():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="2",
        status="fixing",
    )

    updated = apply_fixup_result(state, new_head_sha="def456")

    assert updated.phase == "3"
    assert updated.head_sha == "def456"
    assert updated.status == "waiting_e2e"


def test_apply_no_change_fixup_result_phase2_advances_to_phase3_without_new_head():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="2",
        status="fixing",
    )

    updated = apply_no_change_fixup_result(state)

    assert updated.phase == "3"
    assert updated.head_sha == "abc123"
    assert updated.status == "waiting_e2e"


def test_apply_no_change_fixup_result_phase3_transitions_to_manual_review():
    state = Main2MainState(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        phase="3",
        status="fixing",
    )

    updated = apply_no_change_fixup_result(state)

    assert updated.phase == "done"
    assert updated.head_sha == "abc123"
    assert updated.status == "manual_review"


def test_state_store_registers_and_loads_state():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="waiting_e2e",
        )

        store.register(state)

        loaded = store.get("nv-action/vllm-benchmarks", 148)

        assert loaded == state


def test_state_store_updates_phase_and_head_sha():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="waiting_e2e",
        )
        store.register(state)

        store.update_after_fixup(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            expected_head_sha="abc123",
            new_head_sha="def456",
        )

        loaded = store.get("nv-action/vllm-benchmarks", 148)
        assert loaded is not None
        assert loaded.phase == "3"
        assert loaded.head_sha == "def456"
        assert loaded.status == "waiting_e2e"


def test_state_store_updates_after_no_change_fixup_from_phase2():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="fixing",
        )
        store.register(state)

        store.update_after_no_change_fixup(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            expected_head_sha="abc123",
        )

        loaded = store.get("nv-action/vllm-benchmarks", 148)
        assert loaded is not None
        assert loaded.phase == "3"
        assert loaded.head_sha == "abc123"
        assert loaded.status == "waiting_e2e"


def test_state_store_updates_after_no_change_fixup_from_phase3():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=149,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="def456",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
            phase="3",
            status="fixing",
        )
        store.register(state)

        store.update_after_no_change_fixup(
            repo="nv-action/vllm-benchmarks",
            pr_number=149,
            expected_head_sha="def456",
        )

        loaded = store.get("nv-action/vllm-benchmarks", 149)
        assert loaded is not None
        assert loaded.phase == "done"
        assert loaded.head_sha == "def456"
        assert loaded.status == "manual_review"


def test_state_store_returns_none_for_unknown_pr():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")

        assert store.get("nv-action/vllm-benchmarks", 999) is None


def test_cli_register_persists_state():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "register",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "149",
                "--branch",
                "main2main_auto_2026-03-11_02-02",
                "--head-sha",
                "0ac6428474c21eed75ceacac5b7fc04c58512a95",
                "--old-commit",
                "4034c3d32e30d01639459edd3ab486f56993876d",
                "--new-commit",
                "81939e7733642f583d1731e5c9ef69dcd457b5e5",
                "--phase",
                "2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert '"pr_number": 149' in result.stdout
        store = Main2MainStateStore(state_path)
        assert store.get("nv-action/vllm-benchmarks", 149) is not None


def test_cli_decide_reports_dispatch_fixup():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_12-30",
                head_sha="abc123",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="waiting_e2e",
            )
        )
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "decide",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "148",
                "--head-sha",
                "abc123",
                "--conclusion",
                "failure",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert '"action": "dispatch_fixup"' in result.stdout


def test_cli_update_after_fixup_advances_phase():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_12-30",
                head_sha="abc123",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="fixing",
            )
        )
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "update-after-fixup",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "148",
                "--expected-head-sha",
                "abc123",
                "--new-head-sha",
                "def456",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert '"phase": "3"' in result.stdout
        loaded = store.get("nv-action/vllm-benchmarks", 148)
        assert loaded is not None
        assert loaded.phase == "3"
        assert loaded.head_sha == "def456"


def test_cli_update_after_fixup_rejects_stale_head_sha():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_12-30",
                head_sha="abc123",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="fixing",
            )
        )
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "update-after-fixup",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "148",
                "--expected-head-sha",
                "oldsha",
                "--new-head-sha",
                "def456",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "stale fixup result" in result.stderr


def test_cli_register_from_pr_comment_persists_state():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "register-from-pr-comment",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "149",
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                **dict(),
                "PYTHONPATH": str(repo_root),
                "MAIN2MAIN_TEST_REGISTRATION_COMMENT": """<!-- main2main-register
pr_number=149
branch=main2main_auto_2026-03-11_02-02
head_sha=0ac6428474c21eed75ceacac5b7fc04c58512a95
old_commit=4034c3d32e30d01639459edd3ab486f56993876d
new_commit=81939e7733642f583d1731e5c9ef69dcd457b5e5
phase=2
-->""",
            },
        )

        assert '"action": "register"' in result.stdout
        store = Main2MainStateStore(state_path)
        loaded = store.get("nv-action/vllm-benchmarks", 149)
        assert loaded is not None
        assert loaded.phase == "2"
        assert loaded.status == "waiting_e2e"


def test_cli_run_once_registers_from_comment_and_reconciles():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        repo_root = Path(__file__).resolve().parents[2]

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "run-once",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
            ],
            check=True,
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(repo_root),
                "MAIN2MAIN_TEST_RUN_ONCE": "1",
            },
        )

        assert '"registered": [' in result.stdout
        assert '"149"' in result.stdout or "149" in result.stdout


def test_run_loop_repeats_run_once_and_collects_results():
    calls = []

    class FakeService:
        def run_once(self, repo):
            calls.append(repo)
            return {"ok": len(calls)}

    sleeps = []

    results = run_loop(
        FakeService(),
        "nv-action/vllm-benchmarks",
        interval_seconds=5,
        iterations=3,
        sleep_fn=sleeps.append,
    )

    assert calls == [
        "nv-action/vllm-benchmarks",
        "nv-action/vllm-benchmarks",
        "nv-action/vllm-benchmarks",
    ]
    assert results == [{"ok": 1}, {"ok": 2}, {"ok": 3}]
    assert sleeps == [5, 5]


def test_github_cli_adapter_reads_pr_context():
    commands = []

    def fake_runner(args):
        commands.append(args)
        assert args[:4] == ["gh", "pr", "view", "148"]
        return json_dumps(
            {
                "number": 148,
                "headRefOid": "abc123",
                "headRefName": "main2main_auto_2026-03-11_12-30",
                "body": (
                    "## Summary\n\n"
                    "**Commit range:** `4034c3d32e30d01639459edd3ab486f56993876d`..."
                    "`4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366`\n"
                ),
                "labels": [{"name": "main2main"}, {"name": "ready"}],
                "state": "OPEN",
            }
        )

    adapter = GitHubCliAdapter(fake_runner)
    context = adapter.get_pr_context("nv-action/vllm-benchmarks", 148)

    assert context["pr_number"] == 148
    assert context["head_sha"] == "abc123"
    assert context["branch"] == "main2main_auto_2026-03-11_12-30"
    assert context["metadata"] == PrMetadata(
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
    )
    assert context["labels"] == ["main2main", "ready"]
    assert len(commands) == 1


def test_github_cli_adapter_reads_registration_metadata_from_pr_comments():
    commands = []

    def fake_runner(args):
        commands.append(args)
        assert args == [
            "gh",
            "api",
            "repos/nv-action/vllm-benchmarks/issues/149/comments",
        ]
        return json_dumps(
            [
                {"body": "ordinary comment"},
                {
                    "body": """<!-- main2main-register
pr_number=149
branch=main2main_auto_2026-03-11_02-02
head_sha=0ac6428474c21eed75ceacac5b7fc04c58512a95
old_commit=4034c3d32e30d01639459edd3ab486f56993876d
new_commit=81939e7733642f583d1731e5c9ef69dcd457b5e5
phase=2
-->"""
                },
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)

    metadata = adapter.get_registration_metadata("nv-action/vllm-benchmarks", 149)

    assert metadata == RegistrationMetadata(
        pr_number=149,
        branch="main2main_auto_2026-03-11_02-02",
        head_sha="0ac6428474c21eed75ceacac5b7fc04c58512a95",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
        phase="2",
    )


def test_github_cli_adapter_lists_open_main2main_pr_numbers():
    commands = []

    def fake_runner(args):
        commands.append(args)
        assert args == [
            "gh",
            "pr",
            "list",
            "--repo",
            "nv-action/vllm-benchmarks",
            "--state",
            "open",
            "--label",
            "main2main",
            "--json",
            "number",
        ]
        return json_dumps([{"number": 148}, {"number": 149}])

    adapter = GitHubCliAdapter(fake_runner)

    pr_numbers = adapter.list_open_main2main_pr_numbers("nv-action/vllm-benchmarks")

    assert pr_numbers == [148, 149]


def test_orchestrator_service_registers_pr_from_comment_metadata():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")

        class FakeGitHub:
            def get_registration_metadata(self, repo, pr_number):
                assert repo == "nv-action/vllm-benchmarks"
                assert pr_number == 149
                return RegistrationMetadata(
                    pr_number=149,
                    branch="main2main_auto_2026-03-11_02-02",
                    head_sha="0ac6428474c21eed75ceacac5b7fc04c58512a95",
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                    phase="2",
                )

        service = OrchestratorService(store, FakeGitHub())

        result = service.register_from_pr_comment("nv-action/vllm-benchmarks", 149)

        assert result == {
            "action": "register",
            "phase": "2",
            "reason": "registered from PR comment metadata",
        }
        loaded = store.get("nv-action/vllm-benchmarks", 149)
        assert loaded == Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=149,
            branch="main2main_auto_2026-03-11_02-02",
            head_sha="0ac6428474c21eed75ceacac5b7fc04c58512a95",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
            phase="2",
            status="waiting_e2e",
        )


def test_orchestrator_service_run_once_registers_unknown_prs_and_reconciles_known_prs():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_11-48",
                head_sha="head148",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="3",
                status="waiting_e2e",
            )
        )

        class FakeGitHub:
            def list_open_main2main_pr_numbers(self, repo):
                assert repo == "nv-action/vllm-benchmarks"
                return [148, 149]

            def get_registration_metadata(self, repo, pr_number):
                assert repo == "nv-action/vllm-benchmarks"
                assert pr_number == 149
                return RegistrationMetadata(
                    pr_number=149,
                    branch="main2main_auto_2026-03-11_02-02",
                    head_sha="head149",
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                    phase="2",
                )

            def get_pr_context(self, repo, pr_number):
                if pr_number == 148:
                    return {
                        "pr_number": 148,
                        "head_sha": "head148",
                        "branch": "main2main_auto_2026-03-11_11-48",
                        "state": "OPEN",
                        "labels": ["main2main"],
                        "metadata": PrMetadata(
                            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                        ),
                        "body": "",
                    }
                return {
                    "pr_number": 149,
                    "head_sha": "head149",
                    "branch": "main2main_auto_2026-03-11_02-02",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                    ),
                    "body": "",
                }

            def wait_for_e2e_full(self, *, repo, head_sha):
                if head_sha == "head148":
                    return {
                        "run_id": "1",
                        "head_sha": "head148",
                        "conclusion": "success",
                        "run_url": "https://example/1",
                    }
                return None

            def mark_pr_ready(self, repo, pr_number):
                assert repo == "nv-action/vllm-benchmarks"
                assert pr_number == 148

        service = OrchestratorService(store, FakeGitHub())

        result = service.run_once("nv-action/vllm-benchmarks")

        assert result == {
            "registered": [149],
            "fixup_outcomes": {},
            "reconciled": {
                "148": {
                    "action": "mark_ready",
                    "phase": "3",
                    "reason": "latest E2E-Full run succeeded",
                },
                "149": {
                    "action": "wait",
                    "reason": "e2e-full has not completed yet",
                },
            },
        }


def test_reconcile_dispatch_fixup_marks_state_fixing_with_run_id():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_11-48",
                head_sha="head148",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="waiting_e2e",
            )
        )

        class FakeGitHub:
            def get_pr_context(self, repo, pr_number):
                return {
                    "pr_number": pr_number,
                    "head_sha": "head148",
                    "branch": "main2main_auto_2026-03-11_11-48",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                    ),
                    "body": "",
                }

            def wait_for_e2e_full(self, *, repo, head_sha):
                return {
                    "run_id": "100",
                    "head_sha": "head148",
                    "conclusion": "failure",
                    "run_url": "https://example/e2e/100",
                }

            def dispatch_fixup(self, **kwargs):
                assert kwargs["dispatch_token"] == "token-148"
                return None

            def find_latest_fixup_run(self, *, repo, dispatch_token):
                assert dispatch_token == "token-148"
                return {
                    "run_id": "200",
                    "status": "queued",
                    "run_url": "https://example/fixup/200",
                }

        service = OrchestratorService(store, FakeGitHub(), token_factory=lambda: "token-148")

        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result == {
            "action": "dispatch_fixup",
            "phase": "2",
            "reason": "phase 2 requires another automated fix attempt",
        }
        loaded = store.get("nv-action/vllm-benchmarks", 148)
        assert loaded is not None
        assert loaded.status == "fixing"
        assert loaded.active_fixup_run_id == "200"


def test_reconcile_dispatch_fixup_retries_until_fixup_run_appears():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=152,
                branch="main2main_auto_2026-03-11_08-03",
                head_sha="head152",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="a40ee486f273eaaa885dafd0526f42f3a5b960c9",
                phase="2",
                status="waiting_e2e",
            )
        )

        attempts = {"count": 0}

        class FakeGitHub:
            def get_pr_context(self, repo, pr_number):
                return {
                    "pr_number": pr_number,
                    "head_sha": "head152",
                    "branch": "main2main_auto_2026-03-11_08-03",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="a40ee486f273eaaa885dafd0526f42f3a5b960c9",
                    ),
                    "body": "",
                }

            def wait_for_e2e_full(self, *, repo, head_sha):
                return {
                    "run_id": "300",
                    "head_sha": "head152",
                    "conclusion": "failure",
                    "run_url": "https://example/e2e/300",
                }

            def dispatch_fixup(self, **kwargs):
                assert kwargs["dispatch_token"] == "token-152"
                return None

            def find_latest_fixup_run(self, *, repo, dispatch_token):
                assert dispatch_token == "token-152"
                attempts["count"] += 1
                if attempts["count"] < 3:
                    return None
                return {
                    "run_id": "400",
                    "status": "queued",
                    "run_url": "https://example/fixup/400",
                }

        sleeps = []
        service = OrchestratorService(
            store,
            FakeGitHub(),
            sleep_fn=sleeps.append,
            token_factory=lambda: "token-152",
        )

        result = service.reconcile("nv-action/vllm-benchmarks", 152)

        assert result == {
            "action": "dispatch_fixup",
            "phase": "2",
            "reason": "phase 2 requires another automated fix attempt",
        }
        assert attempts["count"] == 3
        assert sleeps == [2, 2]
        loaded = store.get("nv-action/vllm-benchmarks", 152)
        assert loaded is not None
        assert loaded.status == "fixing"
        assert loaded.active_fixup_run_id == "400"


def test_run_once_applies_completed_fixup_outcome():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_11-48",
                head_sha="head148",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="fixing",
                active_fixup_run_id="200",
            )
        )

        class FakeGitHub:
            def list_open_main2main_pr_numbers(self, repo):
                return [148]

            def get_workflow_run(self, *, repo, run_id):
                assert run_id == "200"
                return {
                    "run_id": "200",
                    "status": "completed",
                    "conclusion": "success",
                    "run_url": "https://example/fixup/200",
                }

            def get_fixup_outcome(self, *, repo, run_id, phase):
                return FixupOutcome(result="changes_pushed", phase="2")

            def get_pr_context(self, repo, pr_number):
                return {
                    "pr_number": 148,
                    "head_sha": "head149",
                    "branch": "main2main_auto_2026-03-11_11-48",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                    ),
                    "body": "",
                }

        service = OrchestratorService(store, FakeGitHub())

        result = service.run_once("nv-action/vllm-benchmarks")

        assert result == {
            "registered": [],
            "fixup_outcomes": {
                "148": {
                    "action": "advance_phase",
                    "phase": "3",
                    "reason": "changes pushed",
                }
            },
            "reconciled": {},
        }
        loaded = store.get("nv-action/vllm-benchmarks", 148)
        assert loaded is not None
        assert loaded.phase == "3"
        assert loaded.status == "waiting_e2e"
        assert loaded.active_fixup_run_id is None


def test_github_cli_adapter_dispatches_fixup_workflow():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return ""

    adapter = GitHubCliAdapter(fake_runner)
    adapter.dispatch_fixup(
        repo="nv-action/vllm-benchmarks",
        pr_number=148,
        branch="main2main_auto_2026-03-11_12-30",
        head_sha="abc123",
        run_id="22901040063",
        run_url="https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
        conclusion="failure",
        phase="2",
        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        dispatch_token="dispatch-148",
    )

    assert commands == [[
        "gh",
        "workflow",
        "run",
        "main2main_auto.yaml",
        "--repo",
        "nv-action/vllm-benchmarks",
        "-f",
        "mode=fixup",
        "-f",
        "pr_number=148",
        "-f",
        "branch=main2main_auto_2026-03-11_12-30",
        "-f",
        "head_sha=abc123",
        "-f",
        "run_id=22901040063",
        "-f",
        "run_url=https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
        "-f",
        "conclusion=failure",
        "-f",
        "phase=2",
        "-f",
        "old_commit=4034c3d32e30d01639459edd3ab486f56993876d",
        "-f",
        "new_commit=4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        "-f",
        "dispatch_token=dispatch-148",
    ]]


def test_github_cli_adapter_finds_fixup_run_by_dispatch_token():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return json.dumps(
            [
                {
                    "databaseId": 10,
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://example/runs/10",
                    "event": "workflow_dispatch",
                    "displayTitle": "Main2Main Auto fixup pr=152 phase=2 token=other-token",
                },
                {
                    "databaseId": 11,
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example/runs/11",
                    "event": "workflow_dispatch",
                    "displayTitle": "Main2Main Auto fixup pr=152 phase=2 token=dispatch-152",
                },
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)

    run = adapter.find_latest_fixup_run(
        repo="nv-action/vllm-benchmarks",
        dispatch_token="dispatch-152",
    )

    assert run == {
        "run_id": "11",
        "status": "in_progress",
        "conclusion": "",
        "run_url": "https://example/runs/11",
    }
    assert commands == [[
        "gh",
        "run",
        "list",
        "--repo",
        "nv-action/vllm-benchmarks",
        "--workflow",
        "main2main_auto.yaml",
        "--json",
        "databaseId,status,conclusion,url,event,displayTitle",
        "-L",
        "20",
    ]]


def test_github_cli_adapter_marks_pr_ready():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return ""

    adapter = GitHubCliAdapter(fake_runner)
    adapter.mark_pr_ready("nv-action/vllm-benchmarks", 148)

    assert commands == [[
        "gh",
        "pr",
        "ready",
        "148",
        "--repo",
        "nv-action/vllm-benchmarks",
    ]]


def test_github_cli_adapter_creates_manual_review_issue():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return "https://github.com/nv-action/vllm-benchmarks/issues/1"

    adapter = GitHubCliAdapter(fake_runner)
    url = adapter.create_manual_review_issue(
        repo="nv-action/vllm-benchmarks",
        title="main2main: manual review needed (2026-03-11)",
        body="manual review body",
    )

    assert url.endswith("/issues/1")
    assert commands == [[
        "gh",
        "issue",
        "create",
        "--repo",
        "nv-action/vllm-benchmarks",
        "--title",
        "main2main: manual review needed (2026-03-11)",
        "--label",
        "main2main",
        "--body",
        "manual review body",
    ]]


def test_github_cli_adapter_waits_for_completed_e2e_full_run():
    commands = []

    def fake_runner(args):
        commands.append(args)
        assert args[:4] == ["gh", "run", "list", "--repo"]
        return json_dumps(
            [
                {
                    "databaseId": 22901040063,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
                }
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)
    result = adapter.wait_for_e2e_full(
        repo="nv-action/vllm-benchmarks",
        head_sha="abc123",
    )

    assert result == {
        "run_id": "22901040063",
        "head_sha": "abc123",
        "conclusion": "failure",
        "run_url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
    }
    assert len(commands) == 1


def test_github_cli_adapter_wait_for_e2e_full_ignores_non_matching_runs():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return json_dumps(
            [
                {
                    "databaseId": 1,
                    "workflowName": "E2E-Full",
                    "headSha": "othersha",
                    "status": "completed",
                    "conclusion": "success",
                    "url": "https://example.invalid/1",
                },
                {
                    "databaseId": 2,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example.invalid/2",
                },
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)
    result = adapter.wait_for_e2e_full(
        repo="nv-action/vllm-benchmarks",
        head_sha="abc123",
    )

    assert result is None


def test_github_cli_adapter_wait_for_e2e_full_uses_latest_matching_run_only():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return json_dumps(
            [
                {
                    "databaseId": 22943110000,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "completed",
                    "conclusion": "failure",
                    "url": "https://example.invalid/old-completed",
                    "createdAt": "2026-03-11T08:00:00Z",
                },
                {
                    "databaseId": 22943116500,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example.invalid/new-in-progress",
                    "createdAt": "2026-03-11T08:10:00Z",
                },
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)
    result = adapter.wait_for_e2e_full(
        repo="nv-action/vllm-benchmarks",
        head_sha="abc123",
    )

    assert result is None


def test_github_cli_adapter_wait_for_e2e_full_waits_if_any_matching_run_is_still_active():
    commands = []

    def fake_runner(args):
        commands.append(args)
        return json_dumps(
            [
                {
                    "databaseId": 22943116541,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "url": "https://example.invalid/completed-cancelled",
                    "createdAt": "2026-03-11T08:15:29Z",
                },
                {
                    "databaseId": 22943116500,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "in_progress",
                    "conclusion": "",
                    "url": "https://example.invalid/in-progress",
                    "createdAt": "2026-03-11T08:15:29Z",
                },
                {
                    "databaseId": 22943116484,
                    "workflowName": "E2E-Full",
                    "headSha": "abc123",
                    "status": "completed",
                    "conclusion": "cancelled",
                    "url": "https://example.invalid/completed-cancelled-older",
                    "createdAt": "2026-03-11T08:15:29Z",
                },
            ]
        )

    adapter = GitHubCliAdapter(fake_runner)
    result = adapter.wait_for_e2e_full(
        repo="nv-action/vllm-benchmarks",
        head_sha="abc123",
    )

    assert result is None


def test_github_cli_adapter_gets_fixup_outcome_from_run_job_output():
    commands = []

    def fake_runner(args):
        commands.append(args)
        if args == [
            "gh",
            "run",
            "view",
            "22936816340",
            "--repo",
            "nv-action/vllm-benchmarks",
            "--json",
            "jobs",
        ]:
            return json_dumps(
                {
                    "jobs": [
                        {"databaseId": 66570006601, "name": "fixup"},
                    ]
                }
            )
        if args == [
            "gh",
            "run",
            "view",
            "22936816340",
            "--repo",
            "nv-action/vllm-benchmarks",
            "--job",
            "66570006601",
        ]:
            return """
✓ fixup in 21m18s (ID 66570006601)
ANNOTATIONS
! No changes after phase 3 fix attempt.
"""
        raise AssertionError(args)

    adapter = GitHubCliAdapter(fake_runner)
    outcome = adapter.get_fixup_outcome(
        repo="nv-action/vllm-benchmarks",
        run_id="22936816340",
        phase="3",
    )

    assert outcome == FixupOutcome(result="no_changes", phase="3")


class FakeGitHubAdapter:
    def __init__(self, pr_context, e2e_result=None):
        self.pr_context = pr_context
        self.e2e_result = e2e_result
        self.calls = []

    def get_pr_context(self, repo, pr_number):
        self.calls.append(("get_pr_context", repo, pr_number))
        return self.pr_context

    def wait_for_e2e_full(self, *, repo, head_sha):
        self.calls.append(("wait_for_e2e_full", repo, head_sha))
        return self.e2e_result

    def mark_pr_ready(self, repo, pr_number):
        self.calls.append(("mark_pr_ready", repo, pr_number))

    def dispatch_fixup(self, **kwargs):
        self.calls.append(("dispatch_fixup", kwargs))

    def find_latest_fixup_run(self, *, repo, dispatch_token):
        self.calls.append(("find_latest_fixup_run", repo, dispatch_token))
        return {
            "run_id": "fixup-1",
            "status": "queued",
            "conclusion": "",
            "run_url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/fixup-1",
        }

    def create_manual_review_issue(self, **kwargs):
        self.calls.append(("create_manual_review_issue", kwargs))
        return "https://github.com/nv-action/vllm-benchmarks/issues/1"

    def update_pr_phase(self, **kwargs):
        self.calls.append(("update_pr_phase", kwargs))

    def get_fixup_outcome(self, *, repo, run_id, phase):
        self.calls.append(("get_fixup_outcome", repo, run_id, phase))
        return FixupOutcome(result="no_changes", phase=phase)

    def get_workflow_run(self, *, repo, run_id):
        self.calls.append(("get_workflow_run", repo, run_id))
        return {
            "run_id": run_id,
            "status": "completed",
            "conclusion": "success",
            "run_url": f"https://github.com/nv-action/vllm-benchmarks/actions/runs/{run_id}",
        }


def test_orchestrator_service_marks_pr_ready_on_success():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="waiting_e2e",
        )
        store.register(state)
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 148,
                "head_sha": "abc123",
                "branch": "main2main_auto_2026-03-11_12-30",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                ),
                "body": "",
            },
            e2e_result={
                "run_id": "22901040063",
                "head_sha": "abc123",
                "conclusion": "success",
                "run_url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
            },
        )

        service = OrchestratorService(store, adapter)
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "mark_ready"
        assert ("mark_pr_ready", "nv-action/vllm-benchmarks", 148) in adapter.calls


def test_orchestrator_service_dispatches_fixup_on_failure():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="waiting_e2e",
        )
        store.register(state)
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 148,
                "head_sha": "abc123",
                "branch": "main2main_auto_2026-03-11_12-30",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                ),
                "body": "",
            },
            e2e_result={
                "run_id": "22901040063",
                "head_sha": "abc123",
                "conclusion": "failure",
                "run_url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
            },
        )

        service = OrchestratorService(store, adapter, token_factory=lambda: "dispatch-148")
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "dispatch_fixup"
        dispatch_calls = [call for call in adapter.calls if call[0] == "dispatch_fixup"]
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0][1]["phase"] == "2"
        assert dispatch_calls[0][1]["dispatch_token"] == "dispatch-148"
        assert ("find_latest_fixup_run", "nv-action/vllm-benchmarks", "dispatch-148") in adapter.calls


def test_orchestrator_service_creates_manual_review_when_done_phase_fails():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="done",
            status="waiting_e2e",
        )
        store.register(state)
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 148,
                "head_sha": "abc123",
                "branch": "main2main_auto_2026-03-11_12-30",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                ),
                "body": "",
            },
            e2e_result={
                "run_id": "22901040063",
                "head_sha": "abc123",
                "conclusion": "failure",
                "run_url": "https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
            },
        )

        service = OrchestratorService(store, adapter)
        service._build_manual_review_issue = lambda **kwargs: {
            "title": "main2main: manual review needed",
            "body": "AI summary for terminal E2E failure",
        }
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "create_manual_review"
        issue_calls = [call for call in adapter.calls if call[0] == "create_manual_review_issue"]
        assert len(issue_calls) == 1
        assert issue_calls[0][1]["body"] == "AI summary for terminal E2E failure"
        updated = store.get("nv-action/vllm-benchmarks", 148)
        assert updated is not None
        assert updated.status == "manual_review"


def test_orchestrator_service_returns_waiting_when_no_completed_e2e_run():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        state = Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            branch="main2main_auto_2026-03-11_12-30",
            head_sha="abc123",
            old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
            new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
            phase="2",
            status="waiting_e2e",
        )
        store.register(state)
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 148,
                "head_sha": "abc123",
                "branch": "main2main_auto_2026-03-11_12-30",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                ),
                "body": "",
            },
            e2e_result=None,
        )

        service = OrchestratorService(store, adapter)
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "wait"


def test_orchestrator_service_advances_to_phase3_when_phase2_fixup_has_no_changes():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_12-30",
                head_sha="abc123",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="fixing",
            )
        )
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 148,
                "head_sha": "abc123",
                "branch": "main2main_auto_2026-03-11_12-30",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                ),
                "body": "",
            },
        )

        service = OrchestratorService(store, adapter)
        result = service.apply_fixup_outcome(
            repo="nv-action/vllm-benchmarks",
            pr_number=148,
            fixup_run_id="22936816340",
        )

        updated = store.get("nv-action/vllm-benchmarks", 148)
        assert result["action"] == "advance_phase"
        assert updated is not None
        assert updated.phase == "3"
        assert updated.head_sha == "abc123"
        assert updated.status == "waiting_e2e"


def test_orchestrator_service_creates_issue_when_phase3_fixup_has_no_changes():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=149,
                branch="main2main_auto_2026-03-11_02-02",
                head_sha="def456",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                phase="3",
                status="fixing",
            )
        )
        adapter = FakeGitHubAdapter(
            pr_context={
                "pr_number": 149,
                "head_sha": "def456",
                "branch": "main2main_auto_2026-03-11_02-02",
                "state": "OPEN",
                "labels": ["main2main"],
                "metadata": PrMetadata(
                    old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                    new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                ),
                "body": "",
            },
        )

        service = OrchestratorService(store, adapter)
        service._build_manual_review_issue = lambda **kwargs: {
            "title": "main2main: manual review needed",
            "body": "AI summary for phase 3 no-change terminal failure",
        }
        result = service.apply_fixup_outcome(
            repo="nv-action/vllm-benchmarks",
            pr_number=149,
            fixup_run_id="22936816137",
        )

        updated = store.get("nv-action/vllm-benchmarks", 149)
        issue_calls = [call for call in adapter.calls if call[0] == "create_manual_review_issue"]
        assert result["action"] == "create_manual_review"
        assert updated is not None
        assert updated.phase == "done"
        assert updated.status == "manual_review"
        assert len(issue_calls) == 1
        assert issue_calls[0][1]["body"] == "AI summary for phase 3 no-change terminal failure"


def test_run_once_skips_terminal_manual_review_state_without_creating_duplicate_issue():
    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=149,
                branch="main2main_auto_2026-03-11_02-02",
                head_sha="def456",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="81939e7733642f583d1731e5c9ef69dcd457b5e5",
                phase="done",
                status="manual_review",
                active_fixup_run_id=None,
            )
        )

        class FakeGitHub:
            def list_open_main2main_pr_numbers(self, repo):
                return [149]

            def create_manual_review_issue(self, **kwargs):
                raise AssertionError("manual review issue should not be created again")

            def get_pr_context(self, repo, pr_number):
                raise AssertionError("terminal manual_review state should not reconcile")

        service = OrchestratorService(store, FakeGitHub())

        result = service.run_once("nv-action/vllm-benchmarks")

        assert result == {
            "registered": [],
            "fixup_outcomes": {},
            "reconciled": {},
        }


def test_run_once_skips_pending_terminal_status():
    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "state.json"
        store = Main2MainStateStore(state_file)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=154,
                branch="branch",
                head_sha="a" * 40,
                old_commit="b" * 40,
                new_commit="c" * 40,
                phase="done",
                status="pending_terminal",
            )
        )

        class FakeGH:
            def list_open_main2main_pr_numbers(self, repo):
                return [154]

        service = OrchestratorService(store, FakeGH())
        result = service.run_once("nv-action/vllm-benchmarks")

        assert result["reconciled"] == {}
        assert result["fixup_outcomes"] == {}


def test_cli_reconcile_reports_wait_when_e2e_not_finished():
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        repo_root = Path(__file__).resolve().parents[2]

        register_result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "register",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "149",
                "--branch",
                "main2main_auto_2026-03-11_02-02",
                "--head-sha",
                "0ac6428474c21eed75ceacac5b7fc04c58512a95",
                "--old-commit",
                "4034c3d32e30d01639459edd3ab486f56993876d",
                "--new-commit",
                "81939e7733642f583d1731e5c9ef69dcd457b5e5",
                "--phase",
                "2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert register_result.returncode == 0

        result = subprocess.run(
            [
                sys.executable,
                str(repo_root / "main2main_orchestrator.py"),
                "reconcile",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "149",
            ],
            check=False,
            capture_output=True,
            text=True,
            env={
                "PYTHONPATH": str(repo_root),
                "MAIN2MAIN_TEST_RUN_ONCE": "1",
            },
        )

        assert result.returncode == 0
        assert '"action": "wait"' in result.stdout
        assert "e2e-full has not completed yet" in result.stdout


def test_reconcile_enqueues_terminal_job_when_callback_provided():
    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "state.json"
        store = Main2MainStateStore(state_file)
        store.register(
            Main2MainState(
                repo="r",
                pr_number=1,
                branch="b",
                head_sha="a" * 40,
                old_commit="b" * 40,
                new_commit="c" * 40,
                phase="done",
                status="waiting_e2e",
            )
        )
        enqueued = []

        class FakeGH:
            def get_pr_context(self, repo, pr_number):
                return {
                    "pr_number": 1,
                    "head_sha": "a" * 40,
                    "branch": "b",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(old_commit="b" * 40, new_commit="c" * 40),
                    "body": "",
                }

            def wait_for_e2e_full(self, *, repo, head_sha):
                return {
                    "run_id": "99",
                    "head_sha": "a" * 40,
                    "conclusion": "failure",
                    "run_url": "u",
                }

        service = OrchestratorService(
            store,
            FakeGH(),
            terminal_enqueue_fn=lambda **kw: enqueued.append(kw),
        )

        result = service.reconcile("r", 1)

        assert result["action"] == "create_manual_review"
        assert len(enqueued) == 1
        assert enqueued[0]["terminal_reason"] == "done_failure"
        state = store.get("r", 1)
        assert state.status == "pending_terminal"


def test_cli_apply_fixup_outcome_updates_state_for_phase2_no_changes(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp_dir:
        state_path = Path(tmp_dir) / "state.json"
        store = Main2MainStateStore(state_path)
        store.register(
            Main2MainState(
                repo="nv-action/vllm-benchmarks",
                pr_number=148,
                branch="main2main_auto_2026-03-11_12-30",
                head_sha="abc123",
                old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                phase="2",
                status="fixing",
            )
        )

        class CliFakeGitHubAdapter:
            def __init__(self):
                self.calls = []

            def get_fixup_outcome(self, *, repo, run_id, phase):
                self.calls.append(("get_fixup_outcome", repo, run_id, phase))
                return FixupOutcome(result="no_changes", phase=phase)

            def create_manual_review_issue(self, **kwargs):
                self.calls.append(("create_manual_review_issue", kwargs))
                return "https://github.com/nv-action/vllm-benchmarks/issues/1"

            def get_pr_context(self, repo, pr_number):
                self.calls.append(("get_pr_context", repo, pr_number))
                return {
                    "pr_number": pr_number,
                    "head_sha": "abc123",
                    "branch": "main2main_auto_2026-03-11_12-30",
                    "state": "OPEN",
                    "labels": ["main2main"],
                    "metadata": PrMetadata(
                        old_commit="4034c3d32e30d01639459edd3ab486f56993876d",
                        new_commit="4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                    ),
                    "body": "",
                }

        fake_adapter = CliFakeGitHubAdapter()
        monkeypatch.setattr(orchestrator, "GitHubCliAdapter", lambda: fake_adapter)

        exit_code = _main(
            [
                "apply-fixup-outcome",
                "--state-file",
                str(state_path),
                "--repo",
                "nv-action/vllm-benchmarks",
                "--pr-number",
                "148",
                "--fixup-run-id",
                "22936816340",
            ]
        )

        updated = store.get("nv-action/vllm-benchmarks", 148)
        assert exit_code == 0
        assert updated is not None
        assert updated.phase == "3"
        assert updated.head_sha == "abc123"


def json_dumps(value):
    import json

    return json.dumps(value)
