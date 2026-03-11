from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from main2main_orchestrator import GitHubCliAdapter, Main2MainState, Main2MainStateStore, OrchestratorService, PrMetadata


def test_reconcile_dispatches_fixup_with_explicit_contract_fields():
    commands = []
    dispatch_token = "dispatch-148"

    def fake_runner(args):
        commands.append(args)
        if args[:4] == ["gh", "pr", "view", "148"]:
            return (
                '{'
                '"number":148,'
                '"headRefOid":"abc123",'
                '"headRefName":"main2main_auto_2026-03-11_12-30",'
                '"body":"## Summary\\n\\n**Commit range:** `4034c3d32e30d01639459edd3ab486f56993876d`...`4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366`\\n",'
                '"labels":[{"name":"main2main"}],'
                '"state":"OPEN"'
                '}'
            )
        if args[:7] == ["gh", "run", "list", "--repo", "nv-action/vllm-benchmarks", "--workflow", "pr_test_full.yaml"]:
            return (
                '['
                '{'
                '"databaseId":22901040063,'
                '"workflowName":"E2E-Full",'
                '"headSha":"abc123",'
                '"status":"completed",'
                '"conclusion":"failure",'
                '"url":"https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063"'
                '}'
                ']'
            )
        if args[:7] == ["gh", "run", "list", "--repo", "nv-action/vllm-benchmarks", "--workflow", "main2main_auto.yaml"]:
            return (
                '['
                '{'
                '"databaseId":22901050000,'
                '"status":"in_progress",'
                '"conclusion":"",'
                '"url":"https://github.com/nv-action/vllm-benchmarks/actions/runs/22901050000",'
                '"event":"workflow_dispatch",'
                f'"displayTitle":"Main2Main Auto fixup pr=148 phase=2 token={dispatch_token}"'
                '}'
                ']'
            )
        return ""

    with tempfile.TemporaryDirectory() as tmp_dir:
        store = Main2MainStateStore(Path(tmp_dir) / "state.json")
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
        service = OrchestratorService(
            store,
            GitHubCliAdapter(fake_runner),
            token_factory=lambda: dispatch_token,
        )

        result = service.reconcile("nv-action/vllm-benchmarks", 148)

    assert result["action"] == "dispatch_fixup"
    dispatch = [cmd for cmd in commands if cmd[:4] == ["gh", "workflow", "run", "main2main_auto.yaml"]]
    assert len(dispatch) == 1
    joined = " ".join(dispatch[0])
    for expected in [
        "mode=fixup",
        "pr_number=148",
        "branch=main2main_auto_2026-03-11_12-30",
        "head_sha=abc123",
        "run_id=22901040063",
        "run_url=https://github.com/nv-action/vllm-benchmarks/actions/runs/22901040063",
        "conclusion=failure",
        "phase=2",
        "old_commit=4034c3d32e30d01639459edd3ab486f56993876d",
        "new_commit=4ff8c3c8f9ece010a1d0e376f5cc1b468b95f366",
        f"dispatch_token={dispatch_token}",
    ]:
        assert expected in joined
