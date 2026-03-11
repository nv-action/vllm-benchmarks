from pathlib import Path

WORKFLOW_PATH = Path(__file__).resolve().parents[2] / '.github' / 'workflows' / 'main2main_auto.yaml'


def read_workflow() -> str:
    return WORKFLOW_PATH.read_text()


def test_workflow_dispatch_declares_fixup_contract_inputs():
    text = read_workflow()
    for key in [
        'mode:',
        'pr_number:',
        'branch:',
        'head_sha:',
        'run_id:',
        'run_url:',
        'conclusion:',
        'phase:',
        'old_commit:',
        'new_commit:',
    ]:
        assert key in text


def test_workflow_no_longer_uses_workflow_run_trigger():
    text = read_workflow()
    assert 'workflow_run:' not in text


def test_fixup_job_uses_explicit_fixup_mode_gate():
    text = read_workflow()
    assert "if: github.event_name == 'workflow_dispatch' && github.event.inputs.mode == 'fixup'" in text
    assert 'Find main2main PR for this run' not in text


def test_fixup_no_longer_tracks_phase_via_labels():
    text = read_workflow()
    assert 'main2main-phase2' not in text
    assert 'main2main-phase3' not in text
    assert '--add-label "${PHASE_LABEL}"' not in text
