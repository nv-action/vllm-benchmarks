import json

import pytest

from tests.e2e.nightly.scripts import profiling


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
