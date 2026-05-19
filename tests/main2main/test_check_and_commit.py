import json
import subprocess
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "main2main"
    / "scripts"
    / "check_and_commit.py"
)


def run_command(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=repo,
        check=False,
        capture_output=True,
        text=True,
    )


def init_repo(repo: Path) -> None:
    run_command(repo, "git", "init")
    run_command(repo, "git", "config", "user.email", "test@example.com")
    run_command(repo, "git", "config", "user.name", "Test User")

    (repo / "README.md").write_text("base\n")
    run_command(repo, "git", "add", "README.md")
    result = run_command(repo, "git", "commit", "-m", "init")
    assert result.returncode == 0, result.stderr


def commit_files(repo: Path) -> list[str]:
    result = run_command(
        repo,
        "git",
        "diff-tree",
        "--no-commit-id",
        "--name-only",
        "-r",
        "HEAD",
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()


def test_check_and_commit_skips_csrc_and_cmake_paths(tmp_path: Path) -> None:
    repo = tmp_path / "vllm-ascend"
    repo.mkdir()
    init_repo(repo)

    source_file = repo / "vllm_ascend" / "platform.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("adapted\n")

    for path in ["csrc/generated.cc", "cmake/generated.cmake"]:
        changed_file = repo / path
        changed_file.parent.mkdir(parents=True, exist_ok=True)
        changed_file.write_text("generated\n")

    result = run_command(
        repo,
        "python3",
        str(SCRIPT),
        "--ascend-path",
        str(repo),
        "--step-id",
        "step-1",
        "--message",
        "main2main: test",
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["files_committed"] == ["vllm_ascend/platform.py"]
    assert "files_skipped" not in payload
    assert commit_files(repo) == ["vllm_ascend/platform.py"]


def test_check_and_commit_leaves_tracked_csrc_and_cmake_changes_uncommitted(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "vllm-ascend"
    repo.mkdir()
    init_repo(repo)

    for path in ["csrc/existing.cc", "cmake/existing.cmake"]:
        changed_file = repo / path
        changed_file.parent.mkdir(parents=True, exist_ok=True)
        changed_file.write_text("base\n")
    run_command(repo, "git", "add", "csrc/existing.cc", "cmake/existing.cmake")
    result = run_command(repo, "git", "commit", "-m", "add build files")
    assert result.returncode == 0, result.stderr

    (repo / "csrc" / "existing.cc").write_text("generated change\n")
    (repo / "cmake" / "existing.cmake").write_text("generated change\n")
    source_file = repo / "vllm_ascend" / "platform.py"
    source_file.parent.mkdir(parents=True)
    source_file.write_text("adapted\n")

    result = run_command(
        repo,
        "python3",
        str(SCRIPT),
        "--ascend-path",
        str(repo),
        "--step-id",
        "step-1",
        "--message",
        "main2main: test",
    )

    assert result.returncode == 0, result.stderr
    assert commit_files(repo) == ["vllm_ascend/platform.py"]

    diff = run_command(repo, "git", "diff", "--name-only", "HEAD", "--", "csrc", "cmake")
    assert diff.stdout.strip().splitlines() == [
        "cmake/existing.cmake",
        "csrc/existing.cc",
    ]


def test_check_and_commit_fails_when_only_skipped_paths_changed(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "vllm-ascend"
    repo.mkdir()
    init_repo(repo)

    changed_file = repo / "csrc" / "generated.cc"
    changed_file.parent.mkdir(parents=True)
    changed_file.write_text("generated\n")

    result = run_command(
        repo,
        "python3",
        str(SCRIPT),
        "--ascend-path",
        str(repo),
        "--step-id",
        "step-1",
        "--message",
        "main2main: test",
    )

    assert result.returncode == 1
    assert "no committable changes" in result.stderr
    assert "csrc/generated.cc" in result.stderr

    count = run_command(repo, "git", "rev-list", "--count", "HEAD")
    assert count.stdout.strip() == "1"
