"""Verify case-declared Shell stage functions and process isolation."""

import os
import time
from pathlib import Path

from build_info import load_build_info, record_build_info
from command_adapter import build_environment, run_command
from context import RunContext


def context_for(tmp_path: Path) -> RunContext:
    case_dir = tmp_path / "software" / "AI" / "sample"
    case_dir.mkdir(parents=True)
    script = case_dir / "sample_test.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "build() {\n"
        "  printf 'function=build\\n'\n"
        "  printf '{\"summary\":{\"qps\":42}}\\n' > \"${RESULTS_DIR}/results.json\"\n"
        "  command -v pip3 > \"${RESULTS_DIR}/pip_path.txt\"\n"
        "  printf '1.0\\n' > \"${PERF_ACTUAL_VERSION_FILE}\"\n"
        "  printf 'build\\n' > \"${RESULTS_DIR}/stage.txt\"\n"
        "}\n"
        "start() { printf 'function=start\\n'; }\n"
        "test() { printf 'function=test\\n'; }\n"
        "stop() { printf 'function=stop\\n'; }\n",
        encoding="utf-8",
    )
    stages = {
        stage: {"script": "sample_test.sh", "function": stage}
        for stage in ("build", "start", "test", "stop")
    }
    case = {
        "name": "sample",
        "category": "AI",
        "execution": {
            "type": "shell-functions",
            "stages": stages,
            "timeout_minutes": 1,
        },
        "outputs": {
            "result": {
                "path": "results.json",
                "stage": "build",
                "format": "json",
                "required": True,
            },
            "pip_path": {
                "path": "pip_path.txt",
                "stage": "build",
                "format": "text",
                "required": True,
            },
        },
    }
    return RunContext(
        root=tmp_path,
        case_path=case_dir / "case.yaml",
        case=case,
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )


def test_environment_contains_framework_context(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    environment = build_environment(context)
    assert environment["EXPECTED_ARCH"] == "x86_64"
    assert "TARGET_ARCH" not in environment
    assert environment["RESULTS_DIR"] == str(context.output_dir)
    assert environment["PERF_ACTUAL_VERSION_FILE"] == str(
        context.work_dir / "actual-version.txt"
    )
    assert environment["PERF_PROCESS_TOKEN"] == "boostkit-perf:unit-run:AI:sample:1.0:x86_64"


def test_framework_actual_version_path_overrides_inherited_environment(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("PERF_ACTUAL_VERSION_FILE", "/tmp/wrong")
    context = context_for(tmp_path)
    assert build_environment(context)["PERF_ACTUAL_VERSION_FILE"] == str(
        context.work_dir / "actual-version.txt"
    )


def test_declared_build_function_streams_and_writes_outputs(tmp_path: Path, capsys) -> None:
    context = context_for(tmp_path)
    result = run_command(context, "build")
    assert result.returncode == 0
    assert "function=build" in capsys.readouterr().out
    assert "function=build" in result.log_path.read_text(encoding="utf-8")
    assert result.log_path.name == "command-build.log"
    assert (context.output_dir / "stage.txt").read_text(encoding="utf-8").strip() == "build"
    pip_path = (context.output_dir / "pip_path.txt").read_text(encoding="utf-8").strip()
    assert pip_path.startswith(str(context.work_dir / "venv" / "bin"))
    record_build_info(context)
    assert load_build_info(context)["actual_version"] == "1.0"


def test_test_stage_uses_raw_output_log_name(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    result = run_command(context, "test")
    assert result.returncode == 0
    assert result.log_path == context.output_dir / "raw-output.log"
    assert result.log_path.read_text(encoding="utf-8") == "function=test\n"


def test_each_stage_calls_only_its_declared_function(tmp_path: Path, capsys) -> None:
    context = context_for(tmp_path)
    result = run_command(context, "start")
    assert result.returncode == 0
    output = capsys.readouterr().out
    assert "function=start" in output
    assert "function=build" not in output
    assert "function=test" not in output
    assert "function=stop" not in output


def test_stage_uses_declared_function_instead_of_inferring_its_name(
    tmp_path: Path, capsys
) -> None:
    context = context_for(tmp_path)
    script = context.case_dir / "sample_test.sh"
    script.write_text(
        "compile_software() { printf 'function=compile_software\\n'; }\n"
        "build() { printf 'ERROR: inferred build function ran\\n'; return 99; }\n",
        encoding="utf-8",
    )
    context.case["execution"]["stages"]["build"]["function"] = "compile_software"
    result = run_command(context, "build")
    assert result.returncode == 0
    output = capsys.readouterr().out
    assert "function=compile_software" in output
    assert "inferred build function ran" not in output


def test_missing_declared_function_fails_without_running_another_stage(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    context.case["execution"]["stages"]["build"]["function"] = "missing_build"
    result = run_command(context, "build")
    assert result.returncode == 10
    log = result.log_path.read_text(encoding="utf-8")
    assert "declared stage function does not exist: missing_build" in log
    assert not (context.output_dir / "stage.txt").exists()


def test_stage_function_return_code_is_preserved(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    script = context.case_dir / "sample_test.sh"
    script.write_text("build() { return 37; }\n", encoding="utf-8")
    result = run_command(context, "build")
    assert result.returncode == 37


def test_timeout_terminates_the_entire_stage_process_group(tmp_path: Path) -> None:
    context = context_for(tmp_path)
    context.case["execution"]["timeout_minutes"] = 0.01
    script = context.case_dir / "sample_test.sh"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        "test() {\n"
        "  sleep 30 &\n"
        "  printf '%s\\n' \"$!\" > \"${RESULTS_DIR}/child.pid\"\n"
        "  wait\n"
        "}\n",
        encoding="utf-8",
    )
    result = run_command(context, "test")
    assert result.returncode == 124
    assert "[TIMEOUT]" in result.log_path.read_text(encoding="utf-8")

    child_pid = int((context.output_dir / "child.pid").read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        raise AssertionError(f"stage child process {child_pid} survived timeout")
