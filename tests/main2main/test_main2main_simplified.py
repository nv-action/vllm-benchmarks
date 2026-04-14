import importlib.util
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


def test_should_create_pr_is_false_when_no_new_commits():
    module = load_module()

    assert module.should_create_pr([]) is False
    assert module.should_create_pr([{"sha": "abc12345", "subject": "feat: something", "body": ""}]) is True
