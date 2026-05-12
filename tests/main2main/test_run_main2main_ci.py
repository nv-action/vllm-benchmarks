import json
import subprocess
import sys
from pathlib import Path


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "main2main"
    / "scripts"
    / "run_main2main_ci.py"
)


def make_fake_ascend_repo(
    tmp_path: Path,
    exit_code: int,
    summary_payload: dict | None = None,
) -> Path:
    ascend_path = tmp_path / "vllm-ascend"
    scripts_dir = ascend_path / ".github" / "workflows" / "scripts"
    scripts_dir.mkdir(parents=True)
    (scripts_dir / "run_suite.py").write_text(
        "\n".join(
            [
                "import json",
                "import os",
                "import sys",
                "print('fake run_suite output')",
                "print('argv=' + json.dumps(sys.argv[1:]))",
                "print('method=' + os.environ.get('VLLM_WORKER_MULTIPROC_METHOD', ''))",
                "print('modelscope=' + os.environ.get('VLLM_USE_MODELSCOPE', ''))",
                f"sys.exit({exit_code})",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if summary_payload is None:
        summary_payload = {
            "run_id": None,
            "run_url": None,
            "good_commit": "good",
            "bad_commit": "bad",
            "failed_test_files_count": 0,
            "failed_test_cases_count": 0,
            "failed_test_files": [],
            "failed_test_cases": [],
            "code_bugs": [],
            "env_flakes": [],
        }
    (scripts_dir / "ci_log_summary.py").write_text(
        "\n".join(
            [
                "import argparse",
                "import json",
                "parser = argparse.ArgumentParser()",
                "parser.add_argument('--log-file')",
                "parser.add_argument('--format')",
                "parser.add_argument('--output')",
                "parser.add_argument('--step-name')",
                "args = parser.parse_args()",
                f"payload = {json.dumps(summary_payload)!r}",
                "with open(args.output, 'w', encoding='utf-8') as f:",
                "    f.write(payload + '\\n')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    return ascend_path


def test_run_main2main_ci_reports_failed_code_bugs(tmp_path: Path):
    ascend_path = make_fake_ascend_repo(
        tmp_path,
        exit_code=7,
        summary_payload={
            "run_id": None,
            "run_url": None,
            "good_commit": "good",
            "bad_commit": "bad",
            "failed_test_files_count": 1,
            "failed_test_cases_count": 1,
            "failed_test_files": ["tests/e2e/test_x.py"],
            "failed_test_cases": ["tests/e2e/test_x.py::test_y"],
            "code_bugs": [{"error_type": "TypeError", "error_message": "bad signature"}],
            "env_flakes": [],
        },
    )
    workspace = tmp_path / "main2main"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ascend-path",
            str(ascend_path),
            "--step-id",
            "step-1",
            "--round",
            "2",
            "--suite",
            "e2e-main2main",
            "--workspace",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 7
    assert "fake run_suite output" not in result.stdout
    assert "method=spawn" not in result.stdout
    assert "modelscope=true" not in result.stdout

    log_path = workspace / "steps" / "step-1" / "ci" / "round-2.log"
    summary_path = workspace / "steps" / "step-1" / "ci" / "round-2-summary.json"
    result_path = workspace / "steps" / "step-1" / "ci" / "round-2-result.json"
    assert "fake run_suite output" in log_path.read_text(encoding="utf-8")

    payload = json.loads(result_path.read_text(encoding="utf-8"))
    assert payload["run_suite_exit_code"] == 7
    assert payload["exit_code"] == 7
    assert payload["ci_result"] == "failed"
    assert payload["passed"] is False
    assert payload["can_commit"] is False
    assert payload["requires_fix"] is True
    assert payload["log_path"] == str(log_path)
    assert payload["summary_path"] == str(summary_path)
    assert payload["code_bugs_count"] == 1
    assert payload["env_flakes_count"] == 0


def test_run_main2main_ci_sets_default_ci_environment(tmp_path: Path):
    ascend_path = make_fake_ascend_repo(tmp_path, exit_code=0)
    workspace = tmp_path / "main2main"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ascend-path",
            str(ascend_path),
            "--step-id",
            "step-1",
            "--suite",
            "e2e-main2main",
            "--workspace",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0
    assert "fake run_suite output" not in result.stdout
    assert "method=spawn" not in result.stdout
    assert "modelscope=true" not in result.stdout
    assert "main2main CI raw log:" in result.stdout
    assert "main2main CI result written to" in result.stdout

    log_path = workspace / "steps" / "step-1" / "ci" / "round-1.log"
    log_text = log_path.read_text(encoding="utf-8")
    assert "fake run_suite output" in log_text
    assert "method=spawn" in log_text
    assert "modelscope=true" in log_text

    payload = json.loads(
        (workspace / "steps" / "step-1" / "ci" / "round-1-result.json").read_text(
            encoding="utf-8",
        )
    )
    assert payload["run_suite_exit_code"] == 0
    assert payload["exit_code"] == 0
    assert payload["ci_result"] == "passed"
    assert payload["passed"] is True
    assert payload["can_commit"] is True


def test_run_main2main_ci_allows_env_flake_pass(tmp_path: Path):
    ascend_path = make_fake_ascend_repo(
        tmp_path,
        exit_code=5,
        summary_payload={
            "run_id": None,
            "run_url": None,
            "good_commit": "good",
            "bad_commit": "bad",
            "failed_test_files_count": 1,
            "failed_test_cases_count": 1,
            "failed_test_files": ["tests/e2e/test_download.py"],
            "failed_test_cases": ["tests/e2e/test_download.py::test_model_download"],
            "code_bugs": [],
            "env_flakes": [
                {
                    "error_type": "OSError",
                    "error_message": "Stale file handle",
                }
            ],
        },
    )
    workspace = tmp_path / "main2main"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ascend-path",
            str(ascend_path),
            "--step-id",
            "step-2",
            "--suite",
            "e2e-main2main",
            "--workspace",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0

    payload = json.loads(
        (workspace / "steps" / "step-2" / "ci" / "round-1-result.json").read_text(
            encoding="utf-8",
        )
    )
    assert payload["run_suite_exit_code"] == 5
    assert payload["exit_code"] == 5
    assert payload["ci_result"] == "env_flake_pass"
    assert payload["passed"] is False
    assert payload["can_commit"] is True
    assert payload["requires_fix"] is False
    assert payload["code_bugs_count"] == 0
    assert payload["env_flakes_count"] == 1


def test_run_main2main_ci_requires_explicit_suite(tmp_path: Path):
    ascend_path = make_fake_ascend_repo(tmp_path, exit_code=0)

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ascend-path",
            str(ascend_path),
            "--step-id",
            "step-1",
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "the following arguments are required: --suite" in result.stderr


def test_run_main2main_ci_forwards_repeated_suite_flags(tmp_path: Path):
    ascend_path = make_fake_ascend_repo(tmp_path, exit_code=0)
    workspace = tmp_path / "main2main"

    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--ascend-path",
            str(ascend_path),
            "--step-id",
            "step-1",
            "--suite",
            "suite-a",
            "--suite",
            "suite-b",
            "--workspace",
            str(workspace),
        ],
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0

    log_text = (workspace / "steps" / "step-1" / "ci" / "round-1.log").read_text(
        encoding="utf-8",
    )
    assert 'argv=["--suite", "suite-a", "--suite", "suite-b", "--continue-on-error"]' in log_text

    payload = json.loads(
        (workspace / "steps" / "step-1" / "ci" / "round-1-result.json").read_text(
            encoding="utf-8",
        )
    )
    assert payload["suite"] == "suite-a+suite-b"
    assert payload["suites"] == ["suite-a", "suite-b"]
    assert payload["command"].count("--suite") == 2
