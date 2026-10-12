import io
import json
import sqlite3
from pathlib import Path

import numpy as np
import pytest

from tools.profiling.steady_state import (
    AisbenchTimingAdapter,
    RequestTiming,
    TimingDataUnavailable,
    analyze_steady_state,
    render_terminal,
    steady_state_summary,
)


def test_timing_adapter_loads_valid_records_and_skips_invalid_ones(tmp_path: Path, caplog):
    details = tmp_path / "dataset_details.jsonl"
    details.write_text(
        "\n".join(
            (
                json.dumps({"uuid": "ok", "time_points": [1, 2, 4], "success": True}),
                json.dumps({"uuid": "failed", "time_points": [2, 3], "success": False}),
                "not-json",
                json.dumps({"uuid": "short", "time_points": [1], "success": True}),
                json.dumps({"time_points": ["private-service:8000", 2]}),
            )
        )
    )

    result = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()

    assert result.timings == [
        RequestTiming("ok", 1.0, 4.0, True),
        RequestTiming("failed", 2.0, 3.0, False),
    ]
    assert result.stats.records_read == 5
    assert result.stats.valid_timings == 2
    assert result.stats.successful_timings == 1
    assert result.stats.invalid_records == 3
    assert "non-numeric value" in caplog.text
    assert "private-service" not in caplog.text


def test_timing_adapter_resolves_sqlite_backed_points(tmp_path: Path):
    database_dir = tmp_path / "db_data"
    database_dir.mkdir()
    database = database_dir / "timings.db"
    buffer = io.BytesIO()
    np.save(buffer, np.array([10.0, 11.5]))
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE numpy_store (id TEXT PRIMARY KEY, arr_blob BLOB)")
        connection.execute("INSERT INTO numpy_store VALUES (?, ?)", ("row-1", buffer.getvalue()))
    (tmp_path / "dataset_details.jsonl").write_text(
        json.dumps(
            {
                "uuid": "db-request",
                "time_points": {"__db_ref__": "row-1"},
                "db_name": database.name,
                "success": True,
            }
        )
    )

    result = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()

    assert result.timings == [RequestTiming("db-request", 10.0, 11.5, True)]


def test_timing_adapter_requires_an_unambiguous_details_file(tmp_path: Path):
    (tmp_path / "a_details.jsonl").touch()
    (tmp_path / "b_details.jsonl").touch()

    with pytest.raises(TimingDataUnavailable, match="refusing to guess"):
        AisbenchTimingAdapter(tmp_path, "missing").load_request_timings()


def test_analyze_steady_state_finds_the_broad_threshold_window():
    result = analyze_steady_state(
        [
            RequestTiming("one", 0, 20, True),
            RequestTiming("two", 1, 21, True),
            RequestTiming("failed", 0, 100, False),
        ],
        target_concurrency=2,
    )

    assert result.status == "found"
    assert result.observed_peak == 2
    assert result.successful_requests == 2
    assert result.steady_start_s == 1
    assert result.steady_end_s == 20
    assert result.completed_at_start == 0
    assert result.completed_at_end == 1


def test_equal_time_handoff_does_not_create_a_false_peak():
    result = analyze_steady_state(
        [RequestTiming("one", 0, 1, True), RequestTiming("two", 1, 2, True)],
        target_concurrency=2,
    )

    assert result.status == "not_found"
    assert result.observed_peak == 1


def test_no_successful_requests_are_unavailable():
    result = analyze_steady_state([RequestTiming("failed", 0, 1, False)], target_concurrency=1)

    assert result.status == "unavailable"
    assert result.reason == "no successful request timing data"


def test_summary_and_terminal_report_expose_the_same_result():
    result = analyze_steady_state(
        [RequestTiming("one", 0, 20, True), RequestTiming("two", 1, 21, True)],
        target_concurrency=2,
    )

    summary = steady_state_summary("case-a", result)
    terminal = render_terminal("case-a", result, width=20)

    assert summary["steady_state"] == {
        "start": {"time_s": 1, "completed_requests": 0},
        "end": {"time_s": 20, "completed_requests": 1},
        "duration_s": 19,
    }
    assert "[STEADY STATE] case-a | FOUND" in terminal
    assert "Steady Duration:          19.00s" in terminal
