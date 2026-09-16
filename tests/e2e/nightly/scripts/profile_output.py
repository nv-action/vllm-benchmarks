import argparse
import json
import os
import shutil
from collections.abc import Callable, Sequence
from pathlib import Path

PROFILE_OUTPUT_ENV = "VLLM_ASCEND_PROFILE_OUTPUT"
PARSED_OUTPUT = "parsed"
RAW_OUTPUT = "raw"
RAW_FALLBACK_OUTPUT = "raw-fallback"
VALID_OUTPUT_MODES = (PARSED_OUTPUT, RAW_OUTPUT)

ASCEND_TRACE_SUFFIX = "_ascend_pt"
PARSED_OUTPUT_DIR = "ASCEND_PROFILER_OUTPUT"
TRACE_VIEW_FILE = "trace_view.json"
ANALYSE_DONE_FILE = "analyse.done"
ANALYSIS_ERROR_FILE = ".profile-analysis-error.txt"
OUTPUT_MODE_FILE = ".profile-output-mode"


def get_profile_output_mode() -> str:
    mode = os.environ.get(PROFILE_OUTPUT_ENV, PARSED_OUTPUT).strip().lower()
    if mode not in VALID_OUTPUT_MODES:
        valid_modes = ", ".join(VALID_OUTPUT_MODES)
        raise ValueError(f"{PROFILE_OUTPUT_ENV} must be one of: {valid_modes}; got {mode!r}")
    return mode


def discover_trace_directories(profile_dir: str | Path) -> list[Path]:
    root = Path(profile_dir)
    return sorted(path for path in root.rglob(f"*{ASCEND_TRACE_SUFFIX}") if path.is_dir())


def _parsed_trace_is_valid(trace_dir: Path) -> bool:
    output_dir = trace_dir / PARSED_OUTPUT_DIR
    trace_view = output_dir / TRACE_VIEW_FILE
    analyse_done = output_dir / ANALYSE_DONE_FILE
    if not analyse_done.is_file() or not trace_view.is_file() or trace_view.stat().st_size == 0:
        return False
    try:
        with trace_view.open(encoding="utf-8") as file:
            json.load(file)
    except (OSError, json.JSONDecodeError):
        return False
    return True


def mark_analysis_failure(profile_dir: str | Path, message: str) -> None:
    root = Path(profile_dir)
    root.mkdir(parents=True, exist_ok=True)
    (root / ANALYSIS_ERROR_FILE).write_text(f"{message.rstrip()}\n", encoding="utf-8")


def analyse_profile_output(
    profile_dir: str | Path,
    *,
    analyser: Callable[..., None] | None = None,
) -> None:
    """Parse every raw Ascend trace after the timed benchmark has finished."""
    if get_profile_output_mode() == RAW_OUTPUT:
        return

    root = Path(profile_dir)
    try:
        trace_dirs = discover_trace_directories(root)
        if not trace_dirs:
            raise RuntimeError(f"no *{ASCEND_TRACE_SUFFIX} trace directories found in {root}")

        if analyser is None:
            from torch_npu.profiler.profiler import analyse

            analyser = analyse

        for trace_dir in trace_dirs:
            if _parsed_trace_is_valid(trace_dir):
                continue
            analyser(str(trace_dir), max_process_number=1)
            if not _parsed_trace_is_valid(trace_dir):
                raise RuntimeError(f"parsed trace validation failed for {trace_dir}")

        (root / ANALYSIS_ERROR_FILE).unlink(missing_ok=True)
    except Exception as exc:
        mark_analysis_failure(root, str(exc))
        raise RuntimeError(f"Failed to parse profiling output in {root}: {exc}") from exc


def _copy_raw_output(root: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {PARSED_OUTPUT_DIR} if PARSED_OUTPUT_DIR in names else set()
        if Path(directory) == root and OUTPUT_MODE_FILE in names:
            ignored.add(OUTPUT_MODE_FILE)
        return ignored

    shutil.copytree(root, destination, dirs_exist_ok=True, ignore=ignore)


def _copy_parsed_output(root: Path, destination: Path, trace_dirs: Sequence[Path]) -> None:
    for trace_dir in trace_dirs:
        target = destination / trace_dir.relative_to(root)
        target.mkdir(parents=True, exist_ok=True)
        shutil.copytree(trace_dir / PARSED_OUTPUT_DIR, target / PARSED_OUTPUT_DIR)
        for pattern in ("profiler_info*.json", "profiler_metadata.json"):
            for metadata in trace_dir.glob(pattern):
                shutil.copy2(metadata, target / metadata.name)


def stage_profile_output(profile_dir: str | Path, output_dir: str | Path, requested_mode: str) -> str:
    """Stage exactly one upload format and return its actual output kind."""
    if requested_mode not in VALID_OUTPUT_MODES:
        valid_modes = ", ".join(VALID_OUTPUT_MODES)
        raise ValueError(f"profile output mode must be one of: {valid_modes}; got {requested_mode!r}")

    root = Path(profile_dir)
    destination = Path(output_dir)
    if not root.is_dir() or not any(root.iterdir()):
        raise RuntimeError(f"Profiling was requested but no output was found in {root}")
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)

    trace_dirs = discover_trace_directories(root)
    parsed_is_valid = bool(trace_dirs) and all(_parsed_trace_is_valid(path) for path in trace_dirs)
    if requested_mode == PARSED_OUTPUT and parsed_is_valid:
        actual_mode = PARSED_OUTPUT
        _copy_parsed_output(root, destination, trace_dirs)
    else:
        actual_mode = RAW_OUTPUT if requested_mode == RAW_OUTPUT else RAW_FALLBACK_OUTPUT
        if actual_mode == RAW_FALLBACK_OUTPUT and not (root / ANALYSIS_ERROR_FILE).exists():
            mark_analysis_failure(root, "parsed profiling output is absent or invalid")
        _copy_raw_output(root, destination)

    (destination / OUTPUT_MODE_FILE).write_text(f"{actual_mode}\n", encoding="utf-8")
    return actual_mode


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prepare nightly profiling output for upload")
    subparsers = parser.add_subparsers(dest="command", required=True)
    stage = subparsers.add_parser("stage", help="stage parsed output or raw fallback")
    stage.add_argument("--profile-dir", required=True)
    stage.add_argument("--output-dir", required=True)
    stage.add_argument("--mode", choices=VALID_OUTPUT_MODES, default=PARSED_OUTPUT)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = _parse_args(argv)
    actual_mode = stage_profile_output(args.profile_dir, args.output_dir, args.mode)
    print(actual_mode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
