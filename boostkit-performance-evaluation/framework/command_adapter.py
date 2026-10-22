#!/usr/bin/env python3
"""Invoke a case-declared Shell function for one software stage."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

from build_info import actual_version_path
from context import RunContext

SHELL_FUNCTION_RUNNER = r"""
script_path="$1"
function_name="$2"
shift 2
source "${script_path}"
if ! declare -F "${function_name}" >/dev/null; then
    printf 'ERROR: declared stage function does not exist: %s in %s\n' \
        "${function_name}" "${script_path}" >&2
    exit 10
fi
"${function_name}"
"""


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    log_path: Path


class StageTerminated(RuntimeError):
    """Raised when the runner asks an active stage to terminate."""


def _terminate_process_group(process: subprocess.Popen[str]) -> None:
    """Stop every process started by one stage, not only its shell parent."""
    if process.poll() is not None:
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    process.wait(timeout=5)


def _raise_stage_terminated(signum: int, _frame: object) -> None:
    raise StageTerminated(f"received signal {signum}")


def _stream_output(stream: TextIO, log: TextIO) -> None:
    """Mirror stage output to the Actions console and its durable stage log."""
    for line in stream:
        sys.stdout.write(line)
        sys.stdout.flush()
        log.write(line)
        log.flush()


def build_environment(context: RunContext, case_venv: Path | None = None) -> dict[str, str]:
    environment = dict(os.environ)
    environment.update({
        "SOFTWARE_VERSION": context.version,
        "EXPECTED_ARCH": context.architecture,
        "RESULTS_DIR": str(context.output_dir),
        "PERF_RUN_ID": context.run_id,
        "PERF_PROCESS_TOKEN": (
            f"boostkit-perf:{context.run_id}:{context.category}:"
            f"{context.software}:{context.version}:{context.architecture}"
        ),
        "PERF_WORK_DIR": str(context.work_dir),
        "PIP_CACHE_DIR": str(context.work_dir / "pip-cache"),
        "XDG_CACHE_HOME": str(context.work_dir / "cache"),
        "CARGO_HOME": str(context.work_dir / "cargo"),
        "CCACHE_DIR": str(context.work_dir / "ccache"),
        "TMPDIR": str(context.work_dir / "tmp"),
        "PYTHONUNBUFFERED": "1",
    })
    if case_venv is not None:
        environment["VIRTUAL_ENV"] = str(case_venv)
        environment["PATH"] = f"{case_venv / 'bin'}:{environment.get('PATH', '')}"
    environment["PERF_ACTUAL_VERSION_FILE"] = str(actual_version_path(context))
    return environment


def run_command(
    context: RunContext,
    stage: str,
) -> CommandResult:
    stage_definition = context.execution["stages"][stage]
    script = (context.case_dir / stage_definition["script"]).resolve()
    function = stage_definition["function"]
    timeout_minutes = float(context.execution.get("timeout_minutes", 180))
    log_name = "raw-output.log" if stage == "test" else f"command-{stage}.log"
    log_path = context.output_dir / log_name
    context.output_dir.mkdir(parents=True, exist_ok=True)
    context.work_dir.mkdir(parents=True, exist_ok=True)
    (context.work_dir / "tmp").mkdir(parents=True, exist_ok=True)
    case_venv = context.work_dir / "venv"
    if not (case_venv / "bin" / "python").is_file():
        subprocess.run(
            [sys.executable, "-m", "venv", str(case_venv)],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    environment = build_environment(context, case_venv)
    command = [
        "bash",
        "--noprofile",
        "--norc",
        "-c",
        SHELL_FUNCTION_RUNNER,
        "framework-stage",
        str(script),
        function,
    ]
    with log_path.open("w", encoding="utf-8") as log:
        process: subprocess.Popen[str] | None = None
        output_thread: threading.Thread | None = None
        previous_sigterm = signal.getsignal(signal.SIGTERM)
        signal.signal(signal.SIGTERM, _raise_stage_terminated)
        try:
            process = subprocess.Popen(
                command,
                cwd=context.case_dir,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
            assert process.stdout is not None
            output_thread = threading.Thread(
                target=_stream_output,
                args=(process.stdout, log),
                name=f"stage-output-{stage}",
            )
            output_thread.start()
            returncode = process.wait(timeout=timeout_minutes * 60)
        except subprocess.TimeoutExpired:
            assert process is not None
            message = f"[TIMEOUT] stage {stage} exceeded {timeout_minutes} minutes\n"
            sys.stderr.write(message)
            sys.stderr.flush()
            log.write(message)
            log.flush()
            _terminate_process_group(process)
            returncode = 124
        except BaseException:
            if process is not None:
                _terminate_process_group(process)
            raise
        finally:
            if output_thread is not None:
                output_thread.join()
            signal.signal(signal.SIGTERM, previous_sigterm)
    return CommandResult(returncode=returncode, log_path=log_path)
