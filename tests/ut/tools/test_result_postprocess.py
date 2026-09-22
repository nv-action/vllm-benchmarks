import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

from tests.e2e.nightly.scripts import result_postprocess


def test_postprocess_invokes_uploader_as_module_from_repo_root(tmp_path: Path, monkeypatch):
    repo_root = tmp_path / "repo"
    script_path = repo_root / "tools" / "upload_to_openlibing.py"
    script_path.parent.mkdir(parents=True)
    script_path.write_text("", encoding="utf-8")
    output_path = tmp_path / "result.json"
    output_path.write_text("{}", encoding="utf-8")
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "", ""))
    monkeypatch.setattr(result_postprocess.subprocess, "run", run)

    result_postprocess._run_postprocess_script(script_path, output_path)

    run.assert_called_once()
    assert run.call_args.args[0] == [
        sys.executable,
        "-m",
        "tools.upload_to_openlibing",
        "--label",
        result_postprocess.UPLOAD_LABEL,
        "--files",
        str(output_path.resolve()),
    ]
    assert run.call_args.kwargs["cwd"] == repo_root.resolve()


def test_upload_module_help_does_not_shadow_standard_library_bisect():
    repo_root = Path(__file__).resolve().parents[3]

    completed = subprocess.run(
        [sys.executable, "-m", "tools.upload_to_openlibing", "--help"],
        cwd=repo_root,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "cannot import name 'bisect'" not in completed.stderr
