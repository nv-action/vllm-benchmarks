import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "run_suite.py"


def load_module():
    scripts_dir = str(SCRIPT_PATH.parent)
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    sys.modules.setdefault("tabulate", types.SimpleNamespace(tabulate=lambda rows, **_kwargs: str(rows)))
    spec = importlib.util.spec_from_file_location("run_suite_under_test", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_repeated_suite_flags_run_all_suite_files_in_order(monkeypatch, tmp_path):
    module = load_module()
    timing_path = tmp_path / "timing.json"
    captured = {}

    suites = {
        "suite-a": [
            module.TestFile("tests/a.py", estimated_time=10),
            module.TestFile("tests/skipped.py", estimated_time=20, is_skipped=True),
        ],
        "suite-b": [
            module.TestFile("tests/b.py", estimated_time=30),
        ],
    }

    monkeypatch.setattr(module, "load_suites", lambda: (suites, set()))
    monkeypatch.setattr(module, "sanity_check", lambda _suites, _upstream_files: None)

    def fake_run_tests(files, continue_on_error):
        captured["files"] = [file.name for file in files]
        captured["continue_on_error"] = continue_on_error
        return 0, [
            module.TestRecord(
                name=file.name,
                passed=True,
                elapsed=1.0,
                estimated=file.estimated_time,
            )
            for file in files
        ]

    monkeypatch.setattr(module, "run_tests", fake_run_tests)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_suite.py",
            "--suite",
            "suite-a",
            "--suite",
            "suite-b",
            "--continue-on-error",
            "--timing-report-json",
            str(timing_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0
    assert captured["files"] == ["tests/a.py", "tests/b.py"]
    assert captured["continue_on_error"] is True

    payload = json.loads(timing_path.read_text(encoding="utf-8"))
    assert payload["suite"] == "suite-a+suite-b"
    assert payload["suites"] == ["suite-a", "suite-b"]
    assert [test["name"] for test in payload["tests"]] == ["tests/a.py", "tests/b.py"]


def test_single_suite_flag_keeps_existing_timing_shape(monkeypatch, tmp_path):
    module = load_module()
    timing_path = tmp_path / "timing.json"

    suites = {
        "suite-a": [
            module.TestFile("tests/a.py", estimated_time=10),
        ],
    }

    monkeypatch.setattr(module, "load_suites", lambda: (suites, set()))
    monkeypatch.setattr(module, "sanity_check", lambda _suites, _upstream_files: None)
    monkeypatch.setattr(
        module,
        "run_tests",
        lambda files, continue_on_error: (
            0,
            [
                module.TestRecord(
                    name=file.name,
                    passed=True,
                    elapsed=1.0,
                    estimated=file.estimated_time,
                )
                for file in files
            ],
        ),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_suite.py",
            "--suite",
            "suite-a",
            "--timing-report-json",
            str(timing_path),
        ],
    )

    with pytest.raises(SystemExit) as exc:
        module.main()

    assert exc.value.code == 0

    payload = json.loads(timing_path.read_text(encoding="utf-8"))
    assert payload["suite"] == "suite-a"
    assert "suites" not in payload
    assert [test["name"] for test in payload["tests"]] == ["tests/a.py"]
