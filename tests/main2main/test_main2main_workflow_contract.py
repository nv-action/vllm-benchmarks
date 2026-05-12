import re
import subprocess
import tempfile
from pathlib import Path

import yaml

WORKFLOWS_DIR = Path(__file__).resolve().parents[2] / ".github" / "workflows"
MAIN_WORKFLOW_PATH = WORKFLOWS_DIR / "schedule_main2main_auto.yaml"


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def load_yaml(path: Path) -> dict:
    return yaml.safe_load(read_text(path))


def workflow_on_section(workflow: dict) -> dict:
    return workflow.get("on") or workflow.get(True) or {}


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


def test_auto_workflow_file_exists():
    assert MAIN_WORKFLOW_PATH.exists()


def test_auto_workflow_declares_only_target_commit_dispatch_input():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    on_section = workflow_on_section(workflow)

    assert on_section["schedule"] == [{"cron": "0 14 * * *"}]
    assert "workflow_dispatch" in on_section
    dispatch_inputs = on_section["workflow_dispatch"]["inputs"]
    assert list(dispatch_inputs) == ["target_commit", "fix_round_limit", "bisect_round_limit"]


def test_auto_workflow_uses_one_long_lived_main_job():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    jobs = workflow["jobs"]

    assert list(jobs) == ["main2main"]
    assert jobs["main2main"]["runs-on"] == "linux-aarch64-a2-4"


def test_auto_workflow_installs_and_configures_claude_cli():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    text = read_text(MAIN_WORKFLOW_PATH)
    steps = workflow["jobs"]["main2main"]["steps"]
    step_names = [step.get("name") for step in steps]
    claude_install_step = next(
        step
        for step in steps
        if step.get("name") == "Install Claude Code CLI"
    )

    assert "Install Claude Code CLI" in text
    assert "Write Claude settings.json" not in step_names
    assert "settings.json" in text
    assert "claude --version" in text
    assert "run-claude-phase" in text
    assert claude_install_step["if"] == "steps.detect.outputs.has_drift == 'true'"
    assert "npm install -g @anthropic-ai/claude-code" in claude_install_step["run"]
    assert 'cat > "$HOME/.claude/settings.json" <<EOF' in claude_install_step["run"]
    assert 'python3 -m json.tool "$HOME/.claude/settings.json" >/dev/null' in claude_install_step["run"]


def test_auto_workflow_uses_cli_loops_not_fixed_round_steps():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "Run fix loop with Claude" in text
    assert "Run bisect-fix loop with Claude" in text
    assert "while true; do" in text
    assert "Claude fix round 1" not in text
    assert "Claude bisect-fix round 1" not in text
    assert "FIX_ROUND_LIMIT" in text
    assert "BISECT_ROUND_LIMIT" in text
    assert "TARGET_COMMIT" in text
    assert 'TARGET="${TARGET_COMMIT}"' in text
    assert "max_fix_rounds" not in text
    assert "max_bisect_rounds" not in text


def test_auto_workflow_still_uses_bisect_workflow_and_helper_cli():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "run-bisect-round" in text
    assert "print-bisect-round-logs" in text
    assert "render-pr-body" in text
    assert "render-manual-review-issue" in text
    assert "run-claude-phase" in text


def test_auto_workflow_keeps_suite_and_log_file_contract():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "--suite e2e-main2main" in text
    assert "/tmp/main2main-test.log" in text
    assert "/tmp/main2main-failure-summary.json" in text
    assert "/tmp/main2main-test-failure-summary.json" not in text
    assert "/tmp/main2main-detect-meta.json" in text
    assert "/tmp/main2main-fix-test-meta.json" in text
    assert "/tmp/main2main-bisect-test-meta.json" in text
    assert "main2main_auto.py" in text
    assert "run-claude-phase" in text
    assert "run-suite-and-summarize" in text
    assert "print-bisect-round-logs" in text
    assert "Determine publish readiness" in text
    assert "Publish draft PR" in text
    assert "Count generated commits" not in text
    assert "Stop when no code adaptation was produced" not in text
    assert "Render manual review issue" not in text
    assert "- name: Render PR body" not in text
    assert "- name: Push branch" not in text
    assert "- name: Create draft PR" not in text
    assert "Create manual review issue" in text


def test_auto_workflow_publish_step_groups_pr_body_and_prints_notices():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    text = read_text(MAIN_WORKFLOW_PATH)

    final_status_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Summarize final status"
    )
    publication_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Determine publish readiness"
    )
    publish_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Publish draft PR"
    )

    publish_script = publish_step["run"]
    assert 'echo "::group::' not in publish_script
    assert 'echo "::endgroup::"' not in publish_script
    assert 'eval "${MAIN2MAIN_LOG_HELPERS}"' in publish_script
    assert 'print_group "PR body" /tmp/main2main-pr-body.md' in publish_script
    assert 'echo "Created draft PR: ${PR_URL}"' in publish_script
    assert "--label main2main" in publish_script
    assert "--label ready" in publish_script
    assert "--label ready-for-test" in publish_script

    assert (
        'echo "::notice::final_status=${FINAL_STATUS}, '
        'fix_rounds_used=${FIX_ROUNDS_USED}, '
        'bisect_rounds_used=${BISECT_ROUNDS_USED}"'
        in final_status_step["run"]
    )
    assert (
        'echo "::notice::commit_count=${COMMIT_COUNT}, '
        'should_publish=${SHOULD_PUBLISH}"'
        in publication_step["run"]
    )
    assert text.count('echo "Created draft PR: ${PR_URL}"') == 1


def test_auto_workflow_does_not_reference_env_context_inside_job_env_expression():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "format('{0}/{1}/.agents/skills/main2main/SKILL.md', github.workspace, env.WORK_REPO_DIR)" not in text
    assert "MAIN2MAIN_SKILL_PATH: ${{ github.workspace }}/vllm-benchmarks/.agents/skills/main2main/SKILL.md" in text


def test_auto_workflow_prints_meta_and_bisect_debug_logs():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "MAIN2MAIN_LOG_HELPERS: |" in text
    assert 'eval "${MAIN2MAIN_LOG_HELPERS}"' in text
    assert text.count("print_group() {") == 1
    assert text.count("print_group_if_nonempty() {") == 1
    assert 'print_group "Claude detect meta" /tmp/main2main-detect-meta.json' in text
    assert 'print_group "Initial test meta" /tmp/main2main-test-meta.json' in text
    assert 'print_group "Claude fix round ${ROUND} meta" "/tmp/main2main-fix-round${ROUND}-meta.json"' in text
    assert 'print_group "Claude fix round ${ROUND} stderr" "/tmp/main2main-fix-round${ROUND}-result.err"' in text
    assert 'print_group "Claude fix round ${ROUND} stdout" "/tmp/main2main-fix-round${ROUND}-result.json"' in text
    assert 'print_group "Fix loop ${ROUND} test meta" /tmp/main2main-fix-test-meta.json' in text
    assert 'print-bisect-round-logs \\' in text
    assert 'if ! python3 "${WORK_REPO_DIR}/.github/workflows/scripts/main2main_auto.py" \\' in text
    assert 'run-bisect-round \\' in text
    assert '--poll-timeout-seconds' not in text
    assert 'print-bisect-round-logs \\' in text
    assert 'Bisect round ${ROUND} setup failed; continue to next bisect round' in text
    assert 'Claude fix round ${ROUND} failed; continue to next round' in text
    assert 'Claude bisect fix round ${ROUND} failed; continue to next bisect round' in text
    assert 'Dirty worktree after Claude fix round ${ROUND}; stop automatic retries' in text
    assert 'Dirty worktree after Claude bisect fix round ${ROUND}; stop automatic retries' in text
    assert text.count('git -C "${WORK_REPO_DIR}" status --porcelain') == 2
    assert 'print_group "Claude bisect fix round ${ROUND} meta" "/tmp/main2main-bisect-fix-round${ROUND}-meta.json"' in text
    assert 'print_group "Claude bisect fix round ${ROUND} stderr" "/tmp/main2main-bisect-fix-round${ROUND}-result.err"' in text
    assert 'print_group "Claude bisect fix round ${ROUND} stdout" "/tmp/main2main-bisect-fix-round${ROUND}-result.json"' in text
    assert 'cat "/tmp/main2main-bisect-round${ROUND}-meta.json"' not in text
    assert 'print_group "Bisect loop ${ROUND} test meta" /tmp/main2main-bisect-test-meta.json' in text
    assert 'print_group_if_nonempty "Initial test failure summary" /tmp/main2main-failure-summary.json' in text
    assert 'print_group_if_nonempty "Fix loop ${ROUND} failure summary" /tmp/main2main-failure-summary.json' in text
    assert 'print_group_if_nonempty "Bisect loop ${ROUND} failure summary" /tmp/main2main-failure-summary.json' in text
    assert 'cat /tmp/main2main-' not in text
    assert 'cp /tmp/main2main-' not in text


def test_auto_workflow_uses_minutes_based_bisect_poll_timeout_env_and_step_timeout():
    text = read_text(MAIN_WORKFLOW_PATH)
    workflow = load_yaml(MAIN_WORKFLOW_PATH)

    assert 'BISECT_POLL_TIMEOUT_MINUTES: "240"' in text

    bisect_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Run bisect-fix loop with Claude"
    )
    assert bisect_step["timeout-minutes"] == 720


def test_auto_workflow_does_not_use_claude_code_action():
    text = read_text(MAIN_WORKFLOW_PATH)
    assert "anthropics/claude-code-action/base-action@" not in text


def test_auto_workflow_does_not_include_fake_claude_support():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "mo" + "ck_claude" not in text
    assert "MO" + "CK_CLAUDE" not in text
    assert "mo" + "ck_claude.py" not in text


def test_auto_workflow_creates_fallback_summary_for_manual_review_issue():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert 'if [ ! -f /tmp/main2main-failure-summary.json ]; then' in text
    assert "MissingFailureSummary" in text
    assert "failure summary was unavailable" in text


def test_auto_workflow_lets_claude_commit_and_workflow_no_longer_commits_directly():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "Do not commit" not in text
    assert "main2main_auto.py" in text
    assert "run-claude-phase" in text
    assert "git commit -F" not in text


def test_auto_workflow_run_steps_are_bash_parseable():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    for step in workflow["jobs"]["main2main"]["steps"]:
        if "run" in step:
            bash_syntax_check(step["run"])
