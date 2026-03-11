from pathlib import Path
import sys
import tempfile
import subprocess

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import main2main_orchestrator as orchestrator

from main2main_orchestrator import (
    GitHubCliAdapter,
    FixupOutcome,
    Main2MainState,
    Main2MainStateStore,
    OrchestratorService,
    PrMetadata,
    _main,
    apply_fixup_result,
    apply_no_change_fixup_result,
    decide_next_action,
    parse_fixup_job_output,
    parse_pr_metadata,
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
                "148",
                "--branch",
                "main2main_auto_2026-03-11_12-30",
                "--head-sha",
                "abc123",
                "--old-commit",
                "4034c3d32e30d01639459edd3ab486f56993876d",
                "--new-commit",
                "4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
                "--phase",
                "2",
            ],
            check=True,
            capture_output=True,
            text=True,
        )

        assert '"pr_number": 148' in result.stdout
        store = Main2MainStateStore(state_path)
        assert store.get("nv-action/vllm-benchmarks", 148) is not None


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

    def create_manual_review_issue(self, **kwargs):
        self.calls.append(("create_manual_review_issue", kwargs))
        return "https://github.com/nv-action/vllm-benchmarks/issues/1"

    def update_pr_phase(self, **kwargs):
        self.calls.append(("update_pr_phase", kwargs))

    def get_fixup_outcome(self, *, repo, run_id, phase):
        self.calls.append(("get_fixup_outcome", repo, run_id, phase))
        return FixupOutcome(result="no_changes", phase=phase)


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

        service = OrchestratorService(store, adapter)
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "dispatch_fixup"
        dispatch_calls = [call for call in adapter.calls if call[0] == "dispatch_fixup"]
        assert len(dispatch_calls) == 1
        assert dispatch_calls[0][1]["phase"] == "2"


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
        result = service.reconcile("nv-action/vllm-benchmarks", 148)

        assert result["action"] == "create_manual_review"
        issue_calls = [call for call in adapter.calls if call[0] == "create_manual_review_issue"]
        assert len(issue_calls) == 1


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
                "148",
                "--branch",
                "main2main_auto_2026-03-11_12-30",
                "--head-sha",
                "abc123",
                "--old-commit",
                "4034c3d32e30d01639459edd3ab486f56993876d",
                "--new-commit",
                "4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
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
                "148",
            ],
            check=False,
            capture_output=True,
            text=True,
        )

        assert result.returncode != 0


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
