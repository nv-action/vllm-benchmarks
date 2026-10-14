"""Timing input formats, event-based steady windows, and public output formats."""

import io
import json
import sqlite3

import numpy as np
import pytest

from tools.profiling.steady_state import (
    AisbenchTimingAdapter,
    RequestTiming,
    TimingDataUnavailable,
    TimingLoadStats,
    analyze_steady_state,
    render_terminal,
    steady_state_summary,
    unavailable_steady_state,
)


def test_load_inline_and_database_timings(tmp_path, caplog):
    database_dir = tmp_path / "db_data"
    database_dir.mkdir()
    blob = io.BytesIO()
    np.save(blob, np.array([11.0, 12.0, 13.0]))
    with sqlite3.connect(database_dir / "timings.db") as connection:
        connection.execute("CREATE TABLE numpy_store (id INTEGER PRIMARY KEY, arr_blob BLOB)")
        connection.execute("INSERT INTO numpy_store VALUES (?, ?)", (1, blob.getvalue()))
    records = [
        {"uuid": "inline", "success": True, "time_points": [10, 11, 12]},
        {"uuid": "database", "success": False, "time_points": {"__db_ref__": 1}, "db_name": "timings.db"},
        {"success": True, "time_points": [2, 1]},
    ]
    (tmp_path / "dataset_details.jsonl").write_text(
        "\n".join(json.dumps(record) for record in records) + "\ninvalid JSON\n\n"
    )
    (tmp_path / "other_details.jsonl").write_text("{}")
    result = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()
    assert result.timings == [RequestTiming("inline", 10, 12, True), RequestTiming("database", 11, 13, False)]
    assert result.stats == TimingLoadStats(records_read=4, valid_timings=2, successful_timings=1, invalid_records=2)
    assert len(caplog.records) == 1
    assert "Skipped 2 invalid" in caplog.text and "line 3" in caplog.text


@pytest.mark.parametrize("file_count", [0, 1, 2])
def test_timing_file_selection(tmp_path, file_count):
    for index in range(file_count):
        (tmp_path / f"other{index}_details.jsonl").write_text('{"time_points":[1,2],"success":true}\n')
    adapter = AisbenchTimingAdapter(tmp_path, "dataset")
    if file_count == 1:
        assert adapter.load_request_timings().stats.valid_timings == 1
    else:
        with pytest.raises(TimingDataUnavailable):
            adapter.load_request_timings()


def test_missing_timing_database_is_unavailable(tmp_path):
    (tmp_path / "dataset_details.jsonl").write_text(
        json.dumps({"time_points": {"__db_ref__": 1}, "db_name": "missing.db"})
    )
    with pytest.raises(TimingDataUnavailable):
        AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()


def test_steady_window_uses_first_and_last_crossings():
    requests = [
        RequestTiming("a", 100, 120, True),
        RequestTiming("b", 101, 115, True),
        RequestTiming("c", 130, 150, True),
        RequestTiming("d", 131, 145, True),
        RequestTiming("failed", 0, 200, False),
    ]
    result = analyze_steady_state(requests, target_concurrency=2, benchmark_zero=100)
    assert result.status == "found"
    assert (result.total_requests, result.successful_requests) == (5, 4)
    assert (result.threshold_concurrency, result.observed_peak) == (2, 2)
    assert (result.steady_start_s, result.steady_end_s, result.steady_duration_s) == (1, 45, 44)
    assert (result.completed_at_start, result.completed_at_end) == (0, 3)
    assert (result.timeline[-1].running_requests, result.timeline[-1].completed_requests) == (0, 4)
    assert result.warning is None
    assert steady_state_summary("load", result)["time_origin"] == "first formal AISBench request (excluding warmup)"
    assert steady_state_summary("load", result)["steady_state"] == {
        "start": {"time_s": 1, "completed_requests": 0},
        "end": {"time_s": 45, "completed_requests": 3},
        "duration_s": 44,
    }


def test_handoffs_and_instant_requests_do_not_inflate_concurrency():
    result = analyze_steady_state(
        [RequestTiming("a", 0, 2, True), RequestTiming("b", 2, 4, True), RequestTiming("instant", 2, 2, True)],
        target_concurrency=2,
        benchmark_zero=0,
    )
    assert result.status == "not_found"
    assert result.observed_peak == 1
    assert result.timeline[-1].completed_requests == 3
    assert min(point.running_requests for point in result.timeline) == 0


def test_idle_time_before_success_does_not_change_steady_window_validation():
    requests = [RequestTiming("a", 1000, 1020, True), RequestTiming("b", 1000, 1020, True)]
    result = analyze_steady_state(requests, 2, benchmark_zero=0)
    assert result.steady_start_s == 1000
    assert result.steady_duration_s == 20
    assert result.warning is None


@pytest.mark.parametrize("concurrency,ratio", [(0, 0.95), (-1, 0.95), (2, 0), (2, 1.1)])
def test_invalid_analysis_parameters(concurrency, ratio):
    with pytest.raises(ValueError):
        analyze_steady_state([], concurrency, ratio, benchmark_zero=0)


@pytest.mark.parametrize("start,end", [(2, 1), (float("nan"), 1), (0, float("inf"))])
def test_loader_rejects_invalid_request_timing(tmp_path, start, end):
    (tmp_path / "dataset_details.jsonl").write_text(json.dumps({"time_points": [start, end], "success": True}))
    result = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()
    assert not result.timings
    assert result.stats.invalid_records == 1


@pytest.mark.parametrize("status", ["found", "not_found", "unavailable"])
def test_summary_and_terminal_output(status):
    if status == "unavailable":
        result = unavailable_steady_state(target_concurrency=2, reason="no timing artifact")
        assert analyze_steady_state([RequestTiming("failed", 0, 1, False)], 2, benchmark_zero=0).status == "unavailable"
    else:
        concurrency = 1 if status == "found" else 2
        result = analyze_steady_state([RequestTiming("request", 0, 1, True)], concurrency, benchmark_zero=0)
    summary = steady_state_summary("load", result)
    assert summary["status"] == status
    assert (summary["steady_state"] is not None) == (status == "found")
    assert json.loads(json.dumps(summary)) == summary
    text = render_terminal("load", result, width=32, request_rate=4, summary_path="summary.json")
    assert text.startswith("::group::") and text.endswith("::endgroup::")
    assert status.upper() in text
    assert "Concurrency Timeline" in text and "Completed Requests" in text
    assert "summary.json" in text
    if status == "found":
        assert result.warning and "WARNING" in text
