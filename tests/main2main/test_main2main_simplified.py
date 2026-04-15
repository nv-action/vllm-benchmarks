import importlib.util
import json
import subprocess
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "main2main_simplified.py"


def load_module():
    spec = importlib.util.spec_from_file_location("main2main_simplified", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_extract_bisect_test_cmd_prefers_failed_test_cases():
    module = load_module()

    summary = {
        "failed_test_cases": [
            "tests/e2e/multicard/4-cards/test_pipeline_parallel.py::test_pipeline_parallel",
            "tests/e2e/multicard/2-cards/test_data_parallel.py::test_data_parallel",
        ],
        "failed_test_files": [
            "tests/e2e/multicard/4-cards/test_pipeline_parallel.py",
        ],
    }

    assert (
        module.extract_bisect_test_cmd(summary)
        == "pytest -sv tests/e2e/multicard/4-cards/test_pipeline_parallel.py::test_pipeline_parallel"
    )


def test_extract_bisect_test_cmd_falls_back_to_failed_test_files():
    module = load_module()

    summary = {
        "failed_test_cases": [],
        "failed_test_files": [
            "tests/e2e/multicard/2-cards/test_expert_parallel.py",
        ],
    }

    assert module.extract_bisect_test_cmd(summary) == "pytest -sv tests/e2e/multicard/2-cards/test_expert_parallel.py"


def test_extract_bisect_test_cmd_supports_real_mock_bisect_target():
    module = load_module()

    summary = {
        "failed_test_cases": [
            "tests/e2e/multicard/4-cards/test_pipeline_parallel.py::test_models_pp2_tp2",
        ],
        "failed_test_files": [
            "tests/e2e/multicard/4-cards/test_pipeline_parallel.py",
        ],
    }

    assert (
        module.extract_bisect_test_cmd(summary)
        == "pytest -sv tests/e2e/multicard/4-cards/test_pipeline_parallel.py::test_models_pp2_tp2"
    )


def test_collect_commit_range_renders_sha_subject_and_body(tmp_path):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    sample = repo / "sample.txt"
    sample.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    sample.write_text("change 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: first change\n\nDetailed explanation for first change."],
        cwd=repo,
        check=True,
    )

    sample.write_text("change 2\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(
        ["git", "commit", "-m", "fix: second change\n\nDetailed explanation for second change."],
        cwd=repo,
        check=True,
    )

    commits = module.collect_commit_range(repo=repo, start_ref=base_ref, end_ref="HEAD")

    assert len(commits) == 2
    assert commits[0]["sha"]
    assert commits[0]["subject"] == "feat: first change"
    assert commits[0]["body"] == "Detailed explanation for first change."
    assert commits[1]["subject"] == "fix: second change"
    assert commits[1]["body"] == "Detailed explanation for second change."


def test_collect_commit_range_handles_commit_without_body(tmp_path):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=repo, check=True)

    sample = repo / "sample.txt"
    sample.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "base commit"], cwd=repo, check=True)
    base_ref = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True
    ).stdout.strip()

    sample.write_text("change 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "sample.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "subject only"], cwd=repo, check=True)

    commits = module.collect_commit_range(repo=repo, start_ref=base_ref, end_ref="HEAD")

    assert len(commits) == 1
    assert commits[0]["subject"] == "subject only"
    assert commits[0]["body"] == ""


def test_append_round_commits_markdown_writes_detect_and_fix_sections(tmp_path):
    module = load_module()

    md_path = tmp_path / "round-commits.md"
    module.append_round_commits_markdown(
        output_path=md_path,
        phase="detect",
        round_index=0,
        commits=[
            {
                "sha": "abc1234511111111111111111111111111111111",
                "subject": "feat: adapt detect path",
                "body": "Handle upstream API rename.",
            }
        ],
    )
    module.append_round_commits_markdown(
        output_path=md_path,
        phase="fix",
        round_index=1,
        commits=[
            {
                "sha": "def6789022222222222222222222222222222222",
                "subject": "fix: address pipeline test",
                "body": "Adjust failing assertion path.",
            }
        ],
    )

    content = md_path.read_text(encoding="utf-8")

    assert "### phase: detect" in content
    assert "round: 0" in content
    assert "sha: `abc1234511111111111111111111111111111111`" in content
    assert "subject: feat: adapt detect path" in content
    assert "body:" in content
    assert "Handle upstream API rename." in content
    assert "### phase: fix" in content
    assert "round: 1" in content


def test_render_detect_prompt_contains_expected_context():
    module = load_module()

    text = module.render_detect_prompt(
        work_repo_dir="vllm-benchmarks",
        vllm_dir="vllm-upstream",
        old_commit="1" * 40,
        new_commit="2" * 40,
    )

    assert "adapt vllm-benchmarks" in text
    assert "- Benchmark repo is checked out at ./vllm-benchmarks" in text
    assert "- Upstream vLLM source is checked out at ./vllm-upstream" in text
    assert f"- OLD_COMMIT={'1' * 40}" in text
    assert f"- NEW_COMMIT={'2' * 40}" in text


def test_render_fix_prompt_contains_round_and_log_path():
    module = load_module()
    log_path = "/tmp/custom-main2main-test.log"

    text = module.render_fix_prompt(
        work_repo_dir="vllm-benchmarks",
        vllm_dir="vllm-upstream",
        old_commit="1" * 40,
        new_commit="2" * 40,
        round_index=3,
        log_path=log_path,
    )

    assert "fix the current main2main test failures" in text
    assert "- Current round=3" in text
    assert f"- Main2Main test log path={log_path}" in text
    assert f"- Use {log_path} as the primary failure-analysis input" in text


def test_render_bisect_fix_prompt_contains_bisect_result_path():
    module = load_module()
    log_path = "/tmp/custom-main2main-test.log"
    bisect_result_path = "/tmp/custom-bisect-output/bisect_result.json"

    text = module.render_bisect_fix_prompt(
        work_repo_dir="vllm-benchmarks",
        vllm_dir="vllm-upstream",
        old_commit="1" * 40,
        new_commit="2" * 40,
        round_index=2,
        log_path=log_path,
        bisect_result_path=bisect_result_path,
    )

    assert "fix the remaining main2main failures based on bisect results" in text
    assert f"- Main2Main test log path={log_path}" in text
    assert f"- Bisect result path={bisect_result_path}" in text
    assert f"- Use {log_path} as the primary failure-analysis input" in text
    assert "- Round=2" in text


def test_render_pr_body_places_summary_at_top_and_embeds_round_markdown():
    module = load_module()

    pr_body = module.render_pr_body(
        old_commit="1111111111111111111111111111111111111111",
        new_commit="2222222222222222222222222222222222222222",
        rounds_markdown=(
            "### phase: detect\n"
            "round: 0\n\n"
            "git_commits:\n"
            "- sha: `abc1234511111111111111111111111111111111`\n"
            "  subject: feat: adapt detect path\n"
            "  body:\n"
            "    Handle upstream API rename.\n"
        ),
    )

    assert pr_body.startswith("Automated adaptation to upstream vLLM main branch changes.")
    assert (
        "Commit range: 1111111111111111111111111111111111111111...2222222222222222222222222222222222222222" in pr_body
    )
    assert "### phase: detect" in pr_body
    assert "subject: feat: adapt detect path" in pr_body
    assert "Handle upstream API rename." in pr_body


def test_run_suite_and_summarize_reports_success_without_summary(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script_dir = repo / ".github" / "workflows" / "scripts"
    script_dir.mkdir(parents=True)

    (script_dir / "run_suite.py").write_text(
        "import sys\n"
        "print('suite ok')\n",
        encoding="utf-8",
    )

    log_path = tmp_path / "suite.log"
    summary_path = tmp_path / "main2main-failure-summary.json"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "run-suite-and-summarize",
            "--work-repo-dir",
            str(repo),
            "--suite",
            "e2e-main2main",
            "--artifact-prefix",
            str(tmp_path / "suite"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["exit_code"] == 0
    assert "suite ok" in log_path.read_text(encoding="utf-8")
    assert not summary_path.exists()


def test_run_suite_and_summarize_generates_summary_on_failure(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    script_dir = repo / ".github" / "workflows" / "scripts"
    script_dir.mkdir(parents=True)

    (script_dir / "run_suite.py").write_text(
        "import sys\n"
        "print('suite failed')\n"
        "sys.exit(3)\n",
        encoding="utf-8",
    )
    (script_dir / "ci_log_summary.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "import sys\n"
        "output = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "output.write_text(json.dumps({'code_bugs': [{'error_type': 'AssertionError'}]}), encoding='utf-8')\n",
        encoding="utf-8",
    )

    log_path = tmp_path / "suite.log"
    summary_path = tmp_path / "main2main-failure-summary.json"

    result = subprocess.run(
        [
            "python3",
            str(SCRIPT_PATH),
            "run-suite-and-summarize",
            "--work-repo-dir",
            str(repo),
            "--suite",
            "e2e-main2main",
            "--artifact-prefix",
            str(tmp_path / "suite"),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["status"] == "failure"
    assert payload["exit_code"] == 3
    assert "suite failed" in log_path.read_text(encoding="utf-8")
    assert json.loads(summary_path.read_text(encoding="utf-8"))["code_bugs"][0]["error_type"] == "AssertionError"


def test_render_manual_review_issue_includes_pr_url_and_bisect_summary():
    module = load_module()

    issue_body = module.render_manual_review_issue(
        pr_url="https://github.com/nv-action/vllm-benchmarks/pull/999",
        old_commit="aaaa",
        new_commit="bbbb",
        summary={
            "failed_test_files": ["tests/e2e/multicard/4-cards/test_pipeline_parallel.py"],
            "failed_test_cases": [],
            "code_bugs": [{"error_type": "AssertionError", "error_message": "still failing"}],
            "env_flakes": [],
        },
        bisect_summary={
            "status": "success",
            "first_bad_commit": "deadbeef",
            "first_bad_commit_url": "https://github.com/vllm-project/vllm/commit/deadbeef",
        },
    )

    assert "https://github.com/nv-action/vllm-benchmarks/pull/999" in issue_body
    assert "`aaaa`...`bbbb`" in issue_body
    assert "AssertionError" in issue_body
    assert "deadbeef" in issue_body


def test_build_bisect_request_id_is_unique_and_stable_shape():
    module = load_module()

    request_id_1 = module.build_bisect_request_id(run_id=123456, round_index=1)
    request_id_2 = module.build_bisect_request_id(run_id=123456, round_index=2)

    assert request_id_1.startswith("main2main-123456-r1-")
    assert request_id_2.startswith("main2main-123456-r2-")
    assert request_id_1 != request_id_2
    assert len(request_id_1.split("-")) >= 4


def test_find_bisect_run_matches_request_id(monkeypatch):
    module = load_module()

    monkeypatch.setattr(
        module,
        "_run_gh_json",
        lambda args: [
            {"databaseId": 100, "displayTitle": "Bisect vLLM request-other"},
            {"databaseId": 101, "displayTitle": "Bisect vLLM request-main2main-123-r1-abcdef12"},
        ],
    )

    run = module.find_bisect_run(
        repo="nv-action/vllm-benchmarks",
        request_id="main2main-123-r1-abcdef12",
    )

    assert run["databaseId"] == 101


def test_find_bisect_run_matches_request_id_with_legacy_gh_name_field(monkeypatch):
    module = load_module()

    monkeypatch.setattr(
        module,
        "_run_gh_json",
        lambda args: [
            {"databaseId": 100, "name": "Bisect vLLM request-other"},
            {"databaseId": 101, "name": "Bisect vLLM request-main2main-123-r1-abcdef12"},
        ],
    )

    run = module.find_bisect_run(
        repo="nv-action/vllm-benchmarks",
        request_id="main2main-123-r1-abcdef12",
    )

    assert run["databaseId"] == 101


def test_find_bisect_run_rejects_mismatched_runs(monkeypatch):
    module = load_module()

    monkeypatch.setattr(
        module,
        "_run_gh_json",
        lambda args: [
            {"databaseId": 100, "displayTitle": "Bisect vLLM request-other"},
        ],
    )

    try:
        module.find_bisect_run(
            repo="nv-action/vllm-benchmarks",
            request_id="main2main-123-r1-abcdef12",
        )
    except ValueError as exc:
        assert "request_id=main2main-123-r1-abcdef12" in str(exc)
    else:
        raise AssertionError("Expected find_bisect_run to reject mismatched runs")


def test_find_bisect_run_falls_back_when_workflow_filtered_query_fails(monkeypatch):
    module = load_module()

    calls = []

    def fake_run_gh_json(args):
        calls.append(args)
        if "--workflow" in args:
            raise subprocess.CalledProcessError(1, ["gh", *args], stderr="workflow lookup failed")
        return [
            {"databaseId": 100, "displayTitle": "Bisect vLLM request-other", "workflowName": "Bisect vLLM"},
            {
                "databaseId": 101,
                "displayTitle": "Bisect vLLM request-main2main-123-r1-abcdef12",
                "workflowName": "Bisect vLLM",
            },
        ]

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    run = module.find_bisect_run(
        repo="nv-action/vllm-benchmarks",
        request_id="main2main-123-r1-abcdef12",
    )

    assert run["databaseId"] == 101
    assert any("--workflow" in call for call in calls)
    assert any("--workflow" not in call for call in calls)


def test_find_bisect_run_falls_back_when_workflow_filtered_query_has_no_match(monkeypatch):
    module = load_module()

    calls = []

    def fake_run_gh_json(args):
        calls.append(args)
        if "--workflow" in args:
            return [
                {"databaseId": 100, "displayTitle": "Bisect vLLM request-other", "workflowName": "Bisect vLLM"},
            ]
        return [
            {"databaseId": 101, "displayTitle": "Bisect vLLM request-main2main-123-r1-abcdef12", "workflowName": "Bisect vLLM"},
        ]

    monkeypatch.setattr(module, "_run_gh_json", fake_run_gh_json)

    run = module.find_bisect_run(
        repo="nv-action/vllm-benchmarks",
        request_id="main2main-123-r1-abcdef12",
    )

    assert run["databaseId"] == 101
    assert any("--workflow" in call for call in calls)
    assert any("--workflow" not in call for call in calls)


def test_print_bisect_round_logs_handles_empty_run_json(tmp_path, capsys):
    module = load_module()

    artifact_prefix = tmp_path / "main2main-bisect-round1"
    (tmp_path / "main2main-bisect-round1-meta.json").write_text('{"request_id":"req"}\n', encoding="utf-8")
    (tmp_path / "main2main-bisect-round1-input.json").write_text('{"test_cmd":"pytest -sv tests/test_demo.py"}\n', encoding="utf-8")
    (tmp_path / "main2main-bisect-round1-run.json").write_text("", encoding="utf-8")
    (tmp_path / "main2main-bisect-round1-find.err").write_text("no match yet\n", encoding="utf-8")
    (tmp_path / "main2main-bisect-round1-runs.json").write_text('[{"databaseId":1}]\n', encoding="utf-8")

    module.print_bisect_round_logs(artifact_prefix=artifact_prefix)

    output = capsys.readouterr().out
    assert '"request_id":"req"' in output
    assert '"test_cmd":"pytest -sv tests/test_demo.py"' in output
    assert "no match yet" in output
    assert '"databaseId":1' in output


def test_run_bisect_round_uses_run_command_helper_contract(tmp_path, monkeypatch):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    script_dir = repo / ".github" / "workflows" / "scripts"
    script_dir.mkdir(parents=True)

    log_path = tmp_path / "main2main-test.log"
    log_path.write_text("suite failed\n", encoding="utf-8")
    failure_summary_path = tmp_path / "main2main-failure-summary.json"
    failure_summary_path.write_text(json.dumps({"failed_test_cases": ["tests/test_demo.py::test_case"]}), encoding="utf-8")
    artifact_prefix = tmp_path / "main2main-bisect-round1"

    def fake_run_command(args, *, cwd=None, stdout_path=None, stderr_path=None, combine_output=False):
        command = args[:3]
        if command == ["python3", str(repo / ".github/workflows/scripts/ci_log_summary.py"), "--log-file"]:
            assert stdout_path is None
            assert stderr_path is None
            assert combine_output is False
            output_path = Path(args[args.index("--output") + 1])
            output_path.write_text(json.dumps({"test_cmd": "pytest -sv tests/test_demo.py::test_case"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[:4] == ["gh", "workflow", "run", "dispatch_main2main_bisect.yaml"]:
            assert stdout_path == tmp_path / "main2main-bisect-round1-dispatch.out"
            assert stderr_path == tmp_path / "main2main-bisect-round1-dispatch.err"
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 2 and args[1].endswith("main2main_simplified.py") and args[2] == "find-bisect-run":
            assert stdout_path == tmp_path / "main2main-bisect-round1-run.json"
            assert stderr_path == tmp_path / "main2main-bisect-round1-find.err"
            stdout_path.write_text(json.dumps({"databaseId": 123, "url": "https://example.com/run/123"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 2 and args[1].endswith("main2main_simplified.py") and args[2] == "poll-bisect-run":
            assert stdout_path == tmp_path / "main2main-bisect-round1-complete.json"
            assert stderr_path == tmp_path / "main2main-bisect-round1-poll.err"
            stdout_path.write_text(json.dumps({"url": "https://example.com/run/123"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["gh", "run", "download"]:
            assert stdout_path == tmp_path / "main2main-bisect-round1-download.out"
            assert stderr_path == tmp_path / "main2main-bisect-round1-download.err"
            output_dir = tmp_path / "main2main-bisect-round1-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "bisect_result.json").write_text(
                json.dumps({"status": "success", "first_bad_commit": "deadbeef"}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "build_bisect_request_id", lambda *, run_id, round_index: f"main2main-{run_id}-r{round_index}-fixed")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)

    result = module.run_bisect_round(
        work_repo_dir=repo,
        github_repo="nv-action/vllm-benchmarks",
        github_run_id="999",
        round_index=1,
        old_commit="oldsha",
        new_commit="newsha",
        log_path=log_path,
        failure_summary_path=failure_summary_path,
        artifact_prefix=artifact_prefix,
    )

    assert result["request_id"] == "main2main-999-r1-fixed"
    assert result["bisect_run_id"] == 123
    assert result["bisect_result_path"].endswith("main2main-bisect-round1-output/bisect_result.json")


def test_run_bisect_round_uses_poll_timeout_minutes_from_env(tmp_path, monkeypatch):
    module = load_module()

    repo = tmp_path / "repo"
    repo.mkdir()
    script_dir = repo / ".github" / "workflows" / "scripts"
    script_dir.mkdir(parents=True)

    log_path = tmp_path / "main2main-test.log"
    log_path.write_text("suite failed\n", encoding="utf-8")
    failure_summary_path = tmp_path / "main2main-failure-summary.json"
    failure_summary_path.write_text(json.dumps({"failed_test_cases": ["tests/test_demo.py::test_case"]}), encoding="utf-8")
    artifact_prefix = tmp_path / "main2main-bisect-round1"

    captured_poll_args = []

    def fake_run_command(args, *, cwd=None, stdout_path=None, stderr_path=None, combine_output=False):
        command = args[:3]
        if command == ["python3", str(repo / ".github/workflows/scripts/ci_log_summary.py"), "--log-file"]:
            output_path = Path(args[args.index("--output") + 1])
            output_path.write_text(json.dumps({"test_cmd": "pytest -sv tests/test_demo.py::test_case"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[:4] == ["gh", "workflow", "run", "dispatch_main2main_bisect.yaml"]:
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 2 and args[1].endswith("main2main_simplified.py") and args[2] == "find-bisect-run":
            assert stdout_path is not None
            stdout_path.write_text(json.dumps({"databaseId": 123, "url": "https://example.com/run/123"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if len(args) >= 2 and args[1].endswith("main2main_simplified.py") and args[2] == "poll-bisect-run":
            captured_poll_args.extend(args)
            assert stdout_path is not None
            stdout_path.write_text(json.dumps({"url": "https://example.com/run/123"}), encoding="utf-8")
            return subprocess.CompletedProcess(args, 0)
        if args[:3] == ["gh", "run", "download"]:
            output_dir = tmp_path / "main2main-bisect-round1-output"
            output_dir.mkdir(parents=True, exist_ok=True)
            (output_dir / "bisect_result.json").write_text(
                json.dumps({"status": "success", "first_bad_commit": "deadbeef"}),
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args, 0)
        raise AssertionError(f"Unexpected command: {args}")

    monkeypatch.setattr(module, "_run_command", fake_run_command)
    monkeypatch.setattr(module, "build_bisect_request_id", lambda *, run_id, round_index: f"main2main-{run_id}-r{round_index}-fixed")
    monkeypatch.setattr(module.time, "sleep", lambda _: None)
    monkeypatch.setenv("BISECT_POLL_TIMEOUT_MINUTES", "180")

    module.run_bisect_round(
        work_repo_dir=repo,
        github_repo="nv-action/vllm-benchmarks",
        github_run_id="999",
        round_index=1,
        old_commit="oldsha",
        new_commit="newsha",
        log_path=log_path,
        failure_summary_path=failure_summary_path,
        artifact_prefix=artifact_prefix,
    )

    assert "--timeout-seconds" in captured_poll_args
    assert "10800" in captured_poll_args


def test_should_create_pr_is_false_when_no_new_commits():
    module = load_module()

    assert module.should_create_pr([]) is False
    assert module.should_create_pr([{"sha": "abc12345", "subject": "feat: something", "body": ""}]) is True
