import json
from pathlib import Path

import pytest

from tests.e2e.nightly.scripts import profile_output, profiling


class _Response:
    def __init__(self, error: Exception | None = None):
        self.error = error

    def raise_for_status(self) -> None:
        if self.error:
            raise self.error


def _enable_profiling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(profiling.PROFILE_ENABLED_ENV, "true")
    monkeypatch.setenv(profiling.PROFILE_DIR_ENV, "/profile")
    monkeypatch.setenv(profiling.PROFILE_WITH_STACK_ENV, "false")
    monkeypatch.setenv(profile_output.PROFILE_OUTPUT_ENV, profile_output.RAW_OUTPUT)


def test_inject_profiler_config_is_disabled_by_default() -> None:
    original = ["--tensor-parallel-size", "8"]

    result = profiling.inject_profiler_config(original)

    assert result == original
    assert result is not original


def test_inject_profiler_config_replaces_both_option_forms(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    original = [
        "--tensor-parallel-size",
        "8",
        "--profiler-config",
        '{"profiler":"old"}',
        '--profiler-config={"profiler":"duplicate"}',
    ]

    result = profiling.inject_profiler_config(original, output_subdir="node-0")

    assert result.count("--profiler-config") == 1
    assert not any(arg.startswith("--profiler-config=") for arg in result)
    config = json.loads(result[result.index("--profiler-config") + 1])
    assert config == {
        "profiler": "torch",
        "torch_profiler_dir": "/profile/node-0",
        "torch_profiler_with_stack": False,
        "ignore_frontend": True,
        "max_iterations": profiling.PROFILE_MAX_ITERATIONS,
    }


def test_inject_profiler_config_can_enable_python_stacks(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(profiling.PROFILE_WITH_STACK_ENV, "1")

    result = profiling.inject_profiler_config([])

    config = json.loads(result[result.index("--profiler-config") + 1])
    assert config["torch_profiler_with_stack"] is True


def test_profile_output_mode_defaults_to_parsed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(profile_output.PROFILE_OUTPUT_ENV, raising=False)

    assert profile_output.get_profile_output_mode() == profile_output.PARSED_OUTPUT


def test_profile_output_mode_rejects_invalid_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(profile_output.PROFILE_OUTPUT_ENV, "both")

    with pytest.raises(ValueError, match=profile_output.PROFILE_OUTPUT_ENV):
        profile_output.get_profile_output_mode()


def test_inject_profiler_config_requires_output_directory(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(profiling.PROFILE_ENABLED_ENV, "true")
    monkeypatch.delenv(profiling.PROFILE_DIR_ENV, raising=False)

    with pytest.raises(ValueError, match=profiling.PROFILE_DIR_ENV):
        profiling.inject_profiler_config([])


def test_profiling_session_starts_before_workload_and_stops_afterward(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    events: list[tuple[str, int | None]] = []

    def post(url: str, timeout: int) -> _Response:
        events.append((url, timeout))
        return _Response()

    monkeypatch.setattr(profiling.requests, "post", post)

    with profiling.profiling_session(["http://server-0/", "http://server-0", "http://server-1"]):
        events.append(("workload", None))

    assert events == [
        ("http://server-0/start_profile", profiling.PROFILE_START_TIMEOUT_SECONDS),
        ("http://server-1/start_profile", profiling.PROFILE_START_TIMEOUT_SECONDS),
        ("workload", None),
        ("http://server-1/stop_profile", profiling.PROFILE_STOP_TIMEOUT_SECONDS),
        ("http://server-0/stop_profile", profiling.PROFILE_STOP_TIMEOUT_SECONDS),
    ]


def test_profiling_session_parses_after_profiler_is_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    monkeypatch.setenv(profile_output.PROFILE_OUTPUT_ENV, profile_output.PARSED_OUTPUT)
    events: list[str] = []

    def post(url: str, timeout: int) -> _Response:
        events.append(url)
        return _Response()

    def analyse(path: str) -> None:
        events.append(f"analyse:{path}")

    monkeypatch.setattr(profiling.requests, "post", post)
    monkeypatch.setattr(profiling, "analyse_profile_output", analyse)

    with profiling.profiling_session(["http://server"]):
        events.append("workload")

    assert events == [
        "http://server/start_profile",
        "workload",
        "http://server/stop_profile",
        "analyse:/profile",
    ]


def test_profiling_session_stops_when_workload_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    urls: list[str] = []

    def post(url: str, timeout: int) -> _Response:
        urls.append(url)
        return _Response()

    monkeypatch.setattr(profiling.requests, "post", post)

    with pytest.raises(RuntimeError, match="benchmark failed"), profiling.profiling_session(["http://server"]):
        raise RuntimeError("benchmark failed")

    assert urls == ["http://server/start_profile", "http://server/stop_profile"]


def test_profiling_session_stops_started_targets_when_start_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)
    urls: list[str] = []

    def post(url: str, timeout: int) -> _Response:
        urls.append(url)
        if url == "http://server-1/start_profile":
            raise RuntimeError("start failed")
        return _Response()

    monkeypatch.setattr(profiling.requests, "post", post)

    with (
        pytest.raises(RuntimeError, match="start failed"),
        profiling.profiling_session(["http://server-0", "http://server-1"]),
    ):
        pytest.fail("workload must not run")

    assert urls == [
        "http://server-0/start_profile",
        "http://server-1/start_profile",
        "http://server-0/stop_profile",
    ]


def test_profiling_session_reports_flush_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)

    def post(url: str, timeout: int) -> _Response:
        if url.endswith("/stop_profile"):
            return _Response(RuntimeError("flush failed"))
        return _Response()

    monkeypatch.setattr(profiling.requests, "post", post)

    with (
        pytest.raises(RuntimeError, match="Failed to flush profiling output"),
        profiling.profiling_session(["http://server"]),
    ):
        pass


def test_profiling_session_does_not_hide_workload_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _enable_profiling(monkeypatch)

    def post(url: str, timeout: int) -> _Response:
        if url.endswith("/stop_profile"):
            return _Response(RuntimeError("flush failed"))
        return _Response()

    monkeypatch.setattr(profiling.requests, "post", post)

    with (
        pytest.raises(ValueError, match="benchmark failed"),
        profiling.profiling_session(["http://server"]),
    ):
        raise ValueError("benchmark failed")


def _write_raw_trace(root: Path, name: str = "worker_ascend_pt") -> Path:
    trace = root / name
    (trace / "PROF_1" / "device_0").mkdir(parents=True)
    (trace / "PROF_1" / "device_0" / "raw.data").write_text("raw", encoding="utf-8")
    return trace


def _write_parsed_trace(trace: Path) -> None:
    output = trace / profile_output.PARSED_OUTPUT_DIR
    output.mkdir(parents=True, exist_ok=True)
    (output / profile_output.TRACE_VIEW_FILE).write_text('{"traceEvents": []}', encoding="utf-8")
    (trace / profile_output.ANALYSE_DONE_FILE).write_text("", encoding="utf-8")
    (trace / "profiler_info_0.json").write_text("{}", encoding="utf-8")


def test_analyse_profile_output_parses_and_validates_each_trace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv(profile_output.PROFILE_OUTPUT_ENV, profile_output.PARSED_OUTPUT)
    traces = [
        _write_raw_trace(tmp_path, "node-0/worker_ascend_pt"),
        _write_raw_trace(tmp_path, "node-1/worker_ascend_pt"),
    ]
    analysed: list[tuple[str, int]] = []

    def analyser(path: str, *, max_process_number: int) -> None:
        analysed.append((path, max_process_number))
        _write_parsed_trace(Path(path))

    profile_output.analyse_profile_output(tmp_path, analyser=analyser)

    assert analysed == [(str(trace), 1) for trace in traces]
    assert not (tmp_path / profile_output.ANALYSIS_ERROR_FILE).exists()


def test_analyse_profile_output_records_validation_failure(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv(profile_output.PROFILE_OUTPUT_ENV, profile_output.PARSED_OUTPUT)
    _write_raw_trace(tmp_path)

    with pytest.raises(RuntimeError, match="parsed trace validation failed"):
        profile_output.analyse_profile_output(tmp_path, analyser=lambda *_args, **_kwargs: None)

    assert "validation failed" in (tmp_path / profile_output.ANALYSIS_ERROR_FILE).read_text()


def test_stage_parsed_output_excludes_raw_data(tmp_path: Path) -> None:
    trace = _write_raw_trace(tmp_path / "source")
    _write_parsed_trace(trace)

    actual = profile_output.stage_profile_output(tmp_path / "source", tmp_path / "staged", "parsed")

    staged_trace = tmp_path / "staged" / trace.relative_to(tmp_path / "source")
    assert actual == profile_output.PARSED_OUTPUT
    assert (staged_trace / profile_output.PARSED_OUTPUT_DIR / profile_output.TRACE_VIEW_FILE).is_file()
    assert not (staged_trace / "PROF_1").exists()


def test_stage_invalid_parsed_output_falls_back_to_raw_only(tmp_path: Path) -> None:
    trace = _write_raw_trace(tmp_path / "source")
    partial_output = trace / profile_output.PARSED_OUTPUT_DIR
    partial_output.mkdir()
    (partial_output / profile_output.TRACE_VIEW_FILE).write_text("invalid", encoding="utf-8")

    actual = profile_output.stage_profile_output(tmp_path / "source", tmp_path / "staged", "parsed")

    staged_trace = tmp_path / "staged" / trace.relative_to(tmp_path / "source")
    assert actual == profile_output.RAW_FALLBACK_OUTPUT
    assert (staged_trace / "PROF_1" / "device_0" / "raw.data").is_file()
    assert not (staged_trace / profile_output.PARSED_OUTPUT_DIR).exists()
    assert (tmp_path / "staged" / profile_output.ANALYSIS_ERROR_FILE).is_file()


def test_stage_raw_output_excludes_existing_parsed_data(tmp_path: Path) -> None:
    trace = _write_raw_trace(tmp_path / "source")
    _write_parsed_trace(trace)

    actual = profile_output.stage_profile_output(tmp_path / "source", tmp_path / "staged", "raw")

    staged_trace = tmp_path / "staged" / trace.relative_to(tmp_path / "source")
    assert actual == profile_output.RAW_OUTPUT
    assert (staged_trace / "PROF_1" / "device_0" / "raw.data").is_file()
    assert not (staged_trace / profile_output.PARSED_OUTPUT_DIR).exists()
