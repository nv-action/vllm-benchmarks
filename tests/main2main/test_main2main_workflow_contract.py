from pathlib import Path
import re
import subprocess
import tempfile

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAIN_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_auto.yaml"
RECONCILE_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_reconcile.yaml"
TERMINAL_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_terminal.yaml"
LEGACY_MANUAL_REVIEW_WORKFLOW_PATH = WORKFLOWS_DIR / "main2main_manual_review.yaml"
BISECT_WORKFLOW_PATH = WORKFLOWS_DIR / "bisect_vllm.yaml"


def read_text(path: Path) -> str:
    return path.read_text()


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(read_text(path))


def resolve_env_value(workflow: dict, value: str) -> str:
    match = re.fullmatch(r"\$\{\{\s*env\.([A-Z0-9_]+)\s*\}\}", value or "")
    if not match:
        return value
    return workflow.get("env", {}).get(match.group(1), value)


def checkout_repositories(path: Path, job_name: str) -> list[str]:
    workflow = load_yaml(path)
    return [
        step["with"].get("repository", "")
        for step in workflow["jobs"][job_name]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


def checkout_paths(path: Path, job_name: str) -> list[str]:
    workflow = load_yaml(path)
    return [
        resolve_env_value(workflow, step["with"].get("path", ""))
        for step in workflow["jobs"][job_name]["steps"]
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]


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


def assert_checkout_precedes_first_run_step(path: Path, job_name: str) -> None:
    workflow = load_yaml(path)
    job = workflow["jobs"][job_name]
    working_directory = job.get("defaults", {}).get("run", {}).get("working-directory")
    if not working_directory:
        return
    steps = job["steps"]
    checkout_index = next(
        index for index, step in enumerate(steps) if str(step.get("uses", "")).startswith("actions/checkout@")
    )
    first_run_index = next(index for index, step in enumerate(steps) if "run" in step)
    assert checkout_index < first_run_index, (
        f"{path.name}:{job_name} sets a job-level working-directory but runs a shell step before checkout"
    )


def assert_working_directory_checkout_precedes_first_run_step(path: Path, job_name: str) -> None:
    workflow = load_yaml(path)
    job = workflow["jobs"][job_name]
    working_directory = job.get("defaults", {}).get("run", {}).get("working-directory")
    if not working_directory:
        return
    steps = job["steps"]
    checkout_index = next(
        index
        for index, step in enumerate(steps)
        if str(step.get("uses", "")).startswith("actions/checkout@")
        and resolve_env_value(workflow, step.get("with", {}).get("path", "")) == working_directory
    )
    first_run_index = next(index for index, step in enumerate(steps) if "run" in step)
    assert checkout_index < first_run_index, (
        f"{path.name}:{job_name} runs in {working_directory} before that checkout path exists"
    )


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
    assert "main2main_ci.py" in text
    assert "prepare-detect-artifacts" in text


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
    assert "main2main_ci.py" in text
    assert "reconcile-pr" in text


def test_reconcile_and_terminal_use_upstream_control_checkout():
    assert checkout_repositories(RECONCILE_WORKFLOW_PATH, "reconcile") == ["nv-action/vllm-benchmarks"]
    assert checkout_repositories(TERMINAL_WORKFLOW_PATH, "terminal") == ["nv-action/vllm-benchmarks"]


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


def test_code_mutating_jobs_use_dual_checkout_and_phase3_prepare_uses_control_only():
    assert checkout_repositories(MAIN_WORKFLOW_PATH, "detect-and-adapt") == [
        "nv-action/vllm-benchmarks",
        "Meihan-chen/vllm-benchmarks",
        "vllm-project/vllm",
    ]
    assert checkout_repositories(MAIN_WORKFLOW_PATH, "fix-phase2") == [
        "nv-action/vllm-benchmarks",
        "Meihan-chen/vllm-benchmarks",
    ]
    assert checkout_repositories(MAIN_WORKFLOW_PATH, "fix-phase3-prepare") == ["nv-action/vllm-benchmarks"]
    assert checkout_repositories(MAIN_WORKFLOW_PATH, "fix-phase3-finalize") == [
        "nv-action/vllm-benchmarks",
        "Meihan-chen/vllm-benchmarks",
    ]


def test_detect_job_branches_from_upstream_main_before_pushing_to_fork():
    text = read_text(MAIN_WORKFLOW_PATH)
    assert 'git remote add upstream "https://github.com/${{ env.UPSTREAM_REPO }}.git"' in text
    assert 'git fetch upstream main' in text
    assert 'git checkout -B "$BRANCH" upstream/main' in text


def test_workflows_use_explicit_control_script_paths():
    for path in [MAIN_WORKFLOW_PATH, RECONCILE_WORKFLOW_PATH, TERMINAL_WORKFLOW_PATH]:
        text = read_text(path)
        assert "python3 .github/workflows/scripts/main2main_ci.py" not in text
        assert "python3 .github/workflows/scripts/ci_log_summary.py" not in text


def test_main2main_bisect_calls_use_per_pr_concurrency():
    text = read_text(BISECT_WORKFLOW_PATH)
    assert "main2main-bisect-pr-{0}" in text or "main2main-bisect-pr-${{ inputs.main2main_pr_number }}" in text


def test_jobs_with_job_level_working_directory_checkout_before_shell_steps():
    assert_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "detect-and-adapt")
    assert_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase2")
    assert_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase3-prepare")
    assert_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase3-finalize")
    assert_checkout_precedes_first_run_step(RECONCILE_WORKFLOW_PATH, "reconcile")
    assert_checkout_precedes_first_run_step(TERMINAL_WORKFLOW_PATH, "terminal")
    assert_working_directory_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "detect-and-adapt")
    assert_working_directory_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase2")
    assert_working_directory_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase3-prepare")
    assert_working_directory_checkout_precedes_first_run_step(MAIN_WORKFLOW_PATH, "fix-phase3-finalize")
    assert_working_directory_checkout_precedes_first_run_step(RECONCILE_WORKFLOW_PATH, "reconcile")
    assert_working_directory_checkout_precedes_first_run_step(TERMINAL_WORKFLOW_PATH, "terminal")


def test_reconcile_main_shell_step_is_bash_parseable():
    workflow = load_yaml(RECONCILE_WORKFLOW_PATH)
    step = next(
        item
        for item in workflow["jobs"]["reconcile"]["steps"]
        if item.get("name") == "Reconcile waiting_e2e and waiting_bisect states"
    )
    bash_syntax_check(step["run"])


def test_main_workflow_critical_shell_steps_are_bash_parseable():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    for job_name, step_name in [
        ("detect-and-adapt", "Publish registration and live state comments"),
        ("fix-phase2", "Push fixes and update comments"),
        ("fix-phase3-prepare", "Build bisect payload and dispatch bisect workflow"),
        ("fix-phase3-finalize", "Push phase 3 fixes or go terminal"),
    ]:
        step = next(item for item in workflow["jobs"][job_name]["steps"] if item.get("name") == step_name)
        bash_syntax_check(step["run"])
