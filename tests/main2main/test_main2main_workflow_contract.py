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
    assert list(dispatch_inputs) == ["target_commit"]


def test_auto_workflow_uses_one_long_lived_main_job():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    jobs = workflow["jobs"]

    assert list(jobs) == ["main2main"]
    assert jobs["main2main"]["runs-on"] == "linux-aarch64-a2-4"


def test_auto_workflow_main_job_uses_bash_for_pipefail_and_pipestatus():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    run_defaults = workflow["jobs"]["main2main"]["defaults"]["run"]

    assert run_defaults["shell"] == "bash -el {0}"
    assert run_defaults["working-directory"] == "${{ github.workspace }}"


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
    assert "Run main2main skill" in text
    assert claude_install_step["if"] == "steps.detect.outputs.has_drift == 'true'"
    assert "npm install -g @anthropic-ai/claude-code" in claude_install_step["run"]
    assert 'cat > "$HOME/.claude/settings.json" <<EOF' in claude_install_step["run"]
    assert 'python3 -m json.tool "$HOME/.claude/settings.json" >/dev/null' in claude_install_step["run"]


def test_auto_workflow_delegates_main2main_control_flow_to_skill():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "Run main2main skill" in text
    assert "Use the main2main skill." in text
    assert "Run fix loop with Claude" not in text
    assert "Run bisect-fix loop with Claude" not in text
    assert "while true; do" not in text
    assert "Claude fix round 1" not in text
    assert "Claude bisect-fix round 1" not in text
    assert "FIX_ROUND_LIMIT" not in text
    assert "BISECT_ROUND_LIMIT" not in text
    assert "TARGET_COMMIT" in text
    assert 'TARGET="${TARGET_COMMIT}"' in text
    assert "max_fix_rounds" not in text
    assert "max_bisect_rounds" not in text


def test_auto_workflow_no_longer_uses_adapt_fix_bisect_helper_cli():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "run-bisect-round" not in text
    assert "print-bisect-round-logs" not in text
    assert "run-claude-phase" not in text
    assert "run-suite-and-summarize" not in text
    assert "render-pr-body" not in text
    assert "render-manual-review-issue" in text


def test_auto_workflow_uses_final_summary_contract():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "MAIN2MAIN_SUITE: e2e-main2main" in text
    assert "/tmp/main2main/final-summary.md" in text
    assert "MAIN2MAIN_WORKSPACE:" in text
    assert "main2main-workspace" in text
    assert "/tmp/main2main/final-summary-parsed.json" in text
    assert "/tmp/main2main/created-commits.json" in text
    assert "/tmp/main2main/created-commits.md" in text
    assert "parse-final-summary" in text
    assert "/tmp/main2main-test.log" not in text
    assert "/tmp/main2main-failure-summary.json" not in text
    assert "/tmp/main2main-test-failure-summary.json" not in text
    assert "/tmp/main2main-detect-meta.json" not in text
    assert "/tmp/main2main-fix-test-meta.json" not in text
    assert "/tmp/main2main-bisect-test-meta.json" not in text
    assert "main2main_auto.py" in text
    assert "Determine publish readiness" in text
    assert "Publish draft PR" in text
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
    assert 'print_group "PR body" /tmp/main2main/final-summary.md' in publish_script
    assert "--body-file /tmp/main2main/final-summary.md" in publish_script
    assert "/tmp/main2main-pr-body.md" not in publish_script
    assert 'echo "Created draft PR: ${PR_URL}"' in publish_script
    assert "--label main2main" not in publish_script
    assert 'LABEL_ARGS=(--add-label main2main --add-label ready --add-label ready-for-test)' in publish_script
    assert "steps.final-status.outputs.status" not in publish_script
    assert "manual-review" not in publish_script
    assert 'gh pr edit "${PR_URL}" "${LABEL_ARGS[@]}"' in publish_script
    assert 'echo "::warning::Failed to add PR labels: ${LABEL_ARGS[*]}"' in publish_script

    assert (
        'echo "::notice::final_status=${FINAL_STATUS}, '
        'reached_commit=${REACHED_COMMIT}, '
        'steps=${STEPS_COMPLETED}/${STEPS_TOTAL}, '
        'commit_count=${COMMIT_COUNT}"'
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
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    work_repo_dir = workflow["env"]["WORK_REPO_DIR"]

    assert "format('{0}/{1}/.agents/skills/main2main/SKILL.md', github.workspace, env.WORK_REPO_DIR)" not in text
    assert f"MAIN2MAIN_SKILL_PATH: ${{{{ github.workspace }}}}/{work_repo_dir}/.agents/skills/main2main/SKILL.md" in text


def test_auto_workflow_prints_main2main_summary_logs():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "MAIN2MAIN_LOG_HELPERS: |" in text
    assert 'eval "${MAIN2MAIN_LOG_HELPERS}"' in text
    assert text.count("print_group() {") == 1
    assert text.count("print_group_if_nonempty() {") == 1
    assert 'print_group "main2main final summary" /tmp/main2main/final-summary.md' in text
    assert 'print_group_if_nonempty "main2main Claude stderr" /tmp/main2main/claude.err' in text
    assert 'print_group_if_nonempty "main2main created commits" /tmp/main2main/created-commits.md' in text
    assert 'cat /tmp/main2main-' not in text
    assert 'cp /tmp/main2main-' not in text


def test_auto_workflow_prints_readable_claude_conversation_stream():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    run_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Run main2main skill"
    )
    script = run_step["run"]

    assert "tee /tmp/main2main/claude.stream.jsonl" in script
    assert "tee /tmp/main2main/claude.stream.jsonl >/dev/null" not in script
    assert 'CLAUDE_PIPE_STATUS=("${PIPESTATUS[@]}")' in script
    assert "print-claude-stream" in script
    assert "--input /tmp/main2main/claude.stream.jsonl" in script
    assert 'print_group_if_nonempty "main2main Claude stderr" /tmp/main2main/claude.err' in script
    assert 'exit "${CLAUDE_STATUS}"' in script


def test_auto_workflow_persists_main2main_workspace_across_steps():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    run_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Run main2main skill"
    )
    final_status_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Summarize final status"
    )
    script = run_step["run"]

    assert workflow["jobs"]["main2main"]["env"]["MAIN2MAIN_WORKSPACE"] == (
        "${{ github.workspace }}/main2main-workspace"
    )
    assert 'rm -rf "${MAIN2MAIN_WORKSPACE}" /tmp/main2main' in script
    assert 'mkdir -p "${MAIN2MAIN_WORKSPACE}"' in script
    assert 'ln -s "${MAIN2MAIN_WORKSPACE}" /tmp/main2main' in script
    assert 'SUMMARY_MD=/tmp/main2main/final-summary.md' in final_status_step["run"]


def test_auto_workflow_cleans_up_heartbeat_process_group_before_exit():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    run_step = next(
        step
        for step in workflow["jobs"]["main2main"]["steps"]
        if step.get("name") == "Run main2main skill"
    )
    script = run_step["run"]

    assert "main2main heartbeat" in script
    assert "sleep 300" in script
    assert "setsid bash -c" in script
    assert 'HEARTBEAT_PID="$!"' in script
    assert 'kill -TERM -- "-${HEARTBEAT_PID}"' in script
    assert "kill ${HEARTBEAT_PID}" not in script
    assert "after claude:" in script
    assert "CLAUDE_STATUS=${CLAUDE_STATUS}" in script
    assert "TEE_STATUS=${TEE_STATUS}" in script
    assert "find /tmp/main2main -maxdepth 3 -type f -print" in script


def test_auto_workflow_removes_bisect_timeout_contract():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "BISECT_POLL_TIMEOUT_MINUTES" not in text
    assert "timeout-minutes: 720" not in text


def test_auto_workflow_does_not_use_claude_code_action():
    text = read_text(MAIN_WORKFLOW_PATH)
    assert "anthropics/claude-code-action/base-action@" not in text


def test_auto_workflow_does_not_include_fake_claude_support():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "mo" + "ck_claude" not in text
    assert "MO" + "CK_CLAUDE" not in text
    assert "mo" + "ck_claude.py" not in text


def test_auto_workflow_uses_final_summary_for_manual_review_issue():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "--summary-md /tmp/main2main/final-summary.md" in text
    assert "--summary-json /tmp/main2main/final-summary.json" not in text
    assert "MissingFailureSummary" not in text
    assert "failure summary was unavailable" not in text


def test_auto_workflow_lets_claude_commit_and_workflow_no_longer_commits_directly():
    text = read_text(MAIN_WORKFLOW_PATH)

    assert "Do not commit" not in text
    assert "main2main_auto.py" in text
    assert "Run main2main skill" in text
    assert "final-summary.md" in text
    assert "git commit -F" not in text


def test_auto_workflow_run_steps_are_bash_parseable():
    workflow = load_yaml(MAIN_WORKFLOW_PATH)
    for step in workflow["jobs"]["main2main"]["steps"]:
        if "run" in step:
            bash_syntax_check(step["run"])
