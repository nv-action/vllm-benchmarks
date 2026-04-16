import json
import os
import subprocess
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "mock_claude.py"


def init_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)


def test_mock_claude_reports_version():
    result = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert "mock-claude" in result.stdout


def test_mock_claude_success_detect_rewrites_old_commit_and_creates_commit(tmp_path):
    bench = tmp_path / "vllm-benchmarks"
    upstream = tmp_path / "vllm-upstream"
    bench.mkdir()
    upstream.mkdir()
    init_repo(bench)
    init_repo(upstream)

    target = bench / "tracked.txt"
    old_commit = "3abf8584432acdd66bad723f9481f379ee1b3ad9"
    new_commit = "55d037e2e5cc56c38a1a4a77a15c347fee380c50"
    target.write_text(f"old={old_commit}\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=bench, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=bench, check=True)

    env = os.environ.copy()
    env.update(
        {
            "MOCK_CLAUDE_MODE": "success",
            "MOCK_CLAUDE_WORK_REPO": str(bench),
            "MOCK_CLAUDE_UPSTREAM_REPO": str(upstream),
            "MOCK_CLAUDE_OLD_COMMIT": old_commit,
            "MOCK_CLAUDE_NEW_COMMIT": new_commit,
        }
    )
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("skill\n", encoding="utf-8")

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "-p",
            "Use Main2Main skill to adapt vllm-benchmarks\n"
            "Context:\n"
            "- Benchmark repo is checked out at ./vllm-benchmarks\n"
            "- Upstream vLLM source is checked out at ./vllm-upstream\n",
            "--session-id",
            "test-session-detect",
            "--append-system-prompt-file",
            str(skill_path),
            "--output-format",
            "json",
            "--allowedTools",
            "Bash(git *)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    assert payload["subtype"] == "success"
    assert payload["session_id"] == "test-session-detect"
    assert new_commit in target.read_text(encoding="utf-8")
    head_subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=bench,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "mock detect" in head_subject


def test_mock_claude_nochange_leaves_repo_clean(tmp_path):
    bench = tmp_path / "vllm-benchmarks"
    upstream = tmp_path / "vllm-upstream"
    bench.mkdir()
    upstream.mkdir()
    init_repo(bench)
    init_repo(upstream)

    target = bench / "tracked.txt"
    target.write_text("unchanged\n", encoding="utf-8")
    subprocess.run(["git", "add", "tracked.txt"], cwd=bench, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=bench, check=True)
    before = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=bench,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()

    log_path = tmp_path / "main2main-test.log"
    log_path.write_text("failure\n", encoding="utf-8")
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("skill\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "MOCK_CLAUDE_MODE": "nochange",
            "MOCK_CLAUDE_PHASE": "fix",
            "MOCK_CLAUDE_LOG_PATH": str(log_path),
            "MOCK_CLAUDE_WORK_REPO": str(bench),
            "MOCK_CLAUDE_UPSTREAM_REPO": str(upstream),
            "MOCK_CLAUDE_OLD_COMMIT": "a" * 40,
            "MOCK_CLAUDE_NEW_COMMIT": "b" * 40,
        }
    )

    subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "-p",
            "Use Main2Main skill to fix the current main2main test failures.\n"
            "Context:\n"
            "- Benchmark repo is checked out at ./vllm-benchmarks\n"
            "- Upstream vLLM source is checked out at ./vllm-upstream\n"
            "- Main2Main test log path="
            f"{log_path}\n",
            "--session-id",
            "test-session-nochange",
            "--append-system-prompt-file",
            str(skill_path),
            "--output-format",
            "json",
            "--allowedTools",
            "Bash(git *)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    after = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=bench,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert before == after


def test_mock_claude_success_fix_creates_config_commit(tmp_path):
    bench = tmp_path / "vllm-benchmarks"
    upstream = tmp_path / "vllm-upstream"
    bench.mkdir()
    upstream.mkdir()
    init_repo(bench)
    init_repo(upstream)

    base = bench / "README.md"
    base.write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=bench, check=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=bench, check=True)

    log_path = tmp_path / "main2main-test.log"
    log_path.write_text("failure\n", encoding="utf-8")
    skill_path = tmp_path / "skill.md"
    skill_path.write_text("skill\n", encoding="utf-8")
    env = os.environ.copy()
    env.update(
        {
            "MOCK_CLAUDE_MODE": "success",
            "MOCK_CLAUDE_PHASE": "fix",
            "MOCK_CLAUDE_LOG_PATH": str(log_path),
            "MOCK_CLAUDE_WORK_REPO": str(bench),
            "MOCK_CLAUDE_UPSTREAM_REPO": str(upstream),
            "MOCK_CLAUDE_OLD_COMMIT": "1" * 40,
            "MOCK_CLAUDE_NEW_COMMIT": "2" * 40,
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT_PATH),
            "-p",
            "Use Main2Main skill to fix the current main2main test failures.\n"
            "Context:\n"
            "- Benchmark repo is checked out at ./vllm-benchmarks\n"
            "- Upstream vLLM source is checked out at ./vllm-upstream\n"
            f"- Main2Main test log path={log_path}\n",
            "--session-id",
            "test-session-fix",
            "--append-system-prompt-file",
            str(skill_path),
            "--output-format",
            "json",
            "--allowedTools",
            "Bash(git *)",
        ],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    payload = json.loads(result.stdout)
    assert payload["subtype"] == "success"
    assert payload["session_id"] == "test-session-fix"
    assert (bench / ".github" / "mock-main2main.yaml").exists()
    head_subject = subprocess.run(
        ["git", "log", "-1", "--pretty=%s"],
        cwd=bench,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert "mock fix" in head_subject
