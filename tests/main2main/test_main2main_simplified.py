import importlib.util
import json
import subprocess
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "scripts"
    / "main2main_simplified.py"
)


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

    assert (
        module.extract_bisect_test_cmd(summary)
        == "pytest -sv tests/e2e/multicard/2-cards/test_expert_parallel.py"
    )


def test_collect_new_commits_renders_short_sha_and_full_message(tmp_path):
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
    base_ref = (
        subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, check=True, text=True, capture_output=True)
        .stdout.strip()
    )

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

    commits = module.collect_new_commits(repo=repo, base_ref=base_ref)

    assert len(commits) == 2
    assert all(len(item["short_sha"]) == 8 for item in commits)
    assert commits[0]["message"] == "feat: first change\n\nDetailed explanation for first change."
    assert commits[1]["message"] == "fix: second change\n\nDetailed explanation for second change."


def test_render_pr_body_includes_commit_range_and_commit_log():
    module = load_module()

    pr_body = module.render_pr_body(
        old_commit="1111111111111111111111111111111111111111",
        new_commit="2222222222222222222222222222222222222222",
        final_status="passed_after_fixes",
        fix_rounds_used=2,
        bisect_rounds_used=1,
        commits=[
            {
                "short_sha": "abc12345",
                "message": "feat: adapt detect path\n\nHandle upstream API rename.",
            },
            {
                "short_sha": "def67890",
                "message": "fix: address pipeline test\n\nAdjust failing assertion path.",
            },
        ],
    )

    assert "1111111111111111111111111111111111111111...2222222222222222222222222222222222222222" in pr_body
    assert "Final status: `passed_after_fixes`" in pr_body
    assert "Fix rounds used: `2`" in pr_body
    assert "Bisect-fix rounds used: `1`" in pr_body
    assert "- `abc12345`" in pr_body
    assert "Handle upstream API rename." in pr_body
    assert "- `def67890`" in pr_body


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
    assert module.should_create_pr([{"short_sha": "abc12345", "message": "feat: something"}]) is True
