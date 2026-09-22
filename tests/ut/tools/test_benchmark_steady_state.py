# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.

import pytest

from tools.benchmark_steady_state import (
    RequestTiming,
    SteadyStateResult,
    TimelinePoint,
    TimingLoadStats,
    _assign_chart_levels,
    analyze_steady_state,
    render_terminal,
    steady_state_summary,
    time_to_column,
)


def _request(request_id: str, start: float, end: float, success: bool = True) -> RequestTiming:
    return RequestTiming(request_id=request_id, start_time=start, end_time=end, success=success)


def test_analyze_ramp_plateau_and_drain():
    result = analyze_steady_state(
        [
            _request("a", 0, 23),
            _request("b", 1, 22),
            _request("c", 2, 21),
            _request("d", 3, 20),
        ],
        target_concurrency=4,
    )

    assert result.status == "found"
    assert result.threshold_concurrency == 4
    assert result.observed_peak == 4
    assert result.steady_start_s == 3
    assert result.completed_at_start == 0
    assert result.steady_end_s == 20
    assert result.completed_at_end == 1
    assert result.steady_duration_s == 17
    assert result.warning is None


def test_threshold_jitter_uses_first_up_and_last_down_crossing():
    result = analyze_steady_state(
        [
            _request("base", 0, 10),
            _request("first", 1, 3),
            _request("second", 3.1, 4),
            _request("last", 4.1, 8),
        ],
        target_concurrency=2,
    )

    assert result.steady_start_s == 1
    assert result.steady_end_s == 8
    assert result.completed_at_end == 3


def test_middle_dip_remains_inside_broad_window_and_in_timeline():
    result = analyze_steady_state(
        [
            _request("a", 0, 20),
            _request("b", 0, 20),
            _request("c", 0, 20),
            _request("before-dip", 0, 5),
            _request("after-dip", 10, 20),
        ],
        target_concurrency=4,
    )

    assert result.steady_start_s == 0
    assert result.steady_end_s == 20
    assert any(point.time_s == 5 and point.running_requests == 3 for point in result.timeline)
    assert any(point.time_s == 10 and point.running_requests == 4 for point in result.timeline)


def test_never_reaching_threshold_is_not_found():
    result = analyze_steady_state(
        [_request("a", 0, 3), _request("b", 1, 2)],
        target_concurrency=3,
    )

    assert result.status == "not_found"
    assert result.observed_peak == 2
    assert result.steady_start_s is None


def test_equal_timestamp_processes_end_before_start_without_false_peak():
    result = analyze_steady_state(
        [
            _request("long", 0, 10),
            _request("ending", 1, 5),
            _request("starting", 5, 9),
        ],
        target_concurrency=2,
    )

    at_handoff = [point.running_requests for point in result.timeline if point.time_s == 5]
    assert at_handoff == [1, 2]
    assert result.observed_peak == 2


def test_failed_request_does_not_contribute_to_concurrency():
    result = analyze_steady_state(
        [
            _request("a", 0, 10),
            _request("failed", 1, 9, success=False),
            _request("b", 2, 8),
        ],
        target_concurrency=2,
    )

    assert result.total_requests == 3
    assert result.successful_requests == 2
    assert result.observed_peak == 2


def test_zero_duration_request_completes_without_negative_concurrency():
    result = analyze_steady_state(
        [_request("instant", 0, 0), _request("normal", 0, 1)],
        target_concurrency=1,
    )

    assert result.observed_peak == 1
    assert all(point.running_requests >= 0 for point in result.timeline)
    assert result.completed_at_start == 1


def test_empty_data_is_unavailable():
    result = analyze_steady_state([], target_concurrency=64)

    assert result.status == "unavailable"
    assert result.threshold_concurrency == 61
    assert result.reason == "no successful request timing data"


def test_rate_controlled_workload_is_skipped():
    result = analyze_steady_state([_request("a", 0, 1)], target_concurrency=1, request_rate=10)

    assert result.status == "skipped"
    assert result.reason == "rate-controlled workload"
    assert result.timeline == ()


def test_short_window_only_emits_warning():
    result = analyze_steady_state(
        [_request("a", 0, 4), _request("b", 1, 3)],
        target_concurrency=2,
    )

    assert result.status == "found"
    assert result.warning is not None
    assert "only 2.00s" in result.warning


def test_renderer_keeps_time_and_completed_coordinates():
    result = analyze_steady_state(
        [
            _request("a", 0, 23),
            _request("b", 1, 22),
            _request("c", 2, 21),
            _request("d", 3, 20),
        ],
        target_concurrency=4,
    )

    output = render_terminal(
        "perf",
        result,
        width=24,
        timing_stats=TimingLoadStats(4, 4, 4, 0),
        timing_directory="outputs/perf",
        request_rate=0,
        summary_path="steady_state/perf/summary.json",
    )

    assert "::group::🟢 [STEADY STATE] perf | FOUND | peak=4/4 | 3.00s→20.00s" in output
    assert "Timing Data" in output
    assert "Records read:          4" in output
    assert "Summary:\n  steady_state/perf/summary.json" in output
    assert "Concurrency Timeline" in output
    assert "Completed Requests" in output
    assert "█" not in output
    assert any(character in output for character in "┌┐└┘")
    assert "3.00s" in output
    assert "completed=0" in output
    assert "20.00s" in output
    assert "completed=1" in output


def test_renderer_uses_unavailable_group_title():
    result = analyze_steady_state([], target_concurrency=4)

    output = render_terminal("perf", result)

    assert "::group::🔴 [STEADY STATE] perf | UNAVAILABLE | timing data unavailable" in output


def test_renderer_uses_not_found_group_title():
    result = analyze_steady_state([_request("a", 0, 3), _request("b", 1, 2)], target_concurrency=3)

    output = render_terminal("perf", result)

    assert "::group::🟡 [STEADY STATE] perf | NOT_FOUND | peak=2<threshold=3" in output


def test_renderer_uses_skipped_group_title():
    result = analyze_steady_state([_request("a", 0, 1)], target_concurrency=1, request_rate=10)

    output = render_terminal("fixed-qps", result, request_rate=10)

    assert "::group::⚪ [STEADY STATE] fixed-qps | SKIPPED | rate-controlled workload" in output


def _chart_rows(output: str, title: str) -> list[str]:
    section = output.split(f"{title}\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    return [line for line in section.splitlines() if " |" in line]


def _synthetic_renderer_result() -> SteadyStateResult:
    return SteadyStateResult(
        status="found",
        total_requests=140,
        successful_requests=140,
        target_concurrency=35,
        threshold_ratio=0.95,
        threshold_concurrency=34,
        observed_peak=35,
        steady_start_s=0.19,
        steady_end_s=196.56,
        steady_duration_s=196.37,
        completed_at_start=0,
        completed_at_end=107,
        warning=None,
        reason=None,
        timeline=(
            TimelinePoint(time_s=0, running_requests=35, completed_requests=0),
            TimelinePoint(time_s=100, running_requests=34, completed_requests=70),
            TimelinePoint(time_s=196.56, running_requests=34, completed_requests=107),
            TimelinePoint(time_s=200, running_requests=0, completed_requests=140),
        ),
    )


def test_renderer_keeps_peak_and_threshold_when_values_are_adjacent():
    output = render_terminal("perf", _synthetic_renderer_result())
    rows = _chart_rows(output, "Concurrency Timeline")
    peak_row = next(index for index, line in enumerate(rows) if line.lstrip().startswith("35 |"))
    threshold_row = next(index for index, line in enumerate(rows) if line.lstrip().startswith("34 |"))

    assert peak_row != threshold_row
    assert "threshold" in rows[threshold_row]


def test_renderer_keeps_not_found_peak_below_threshold():
    result = analyze_steady_state(
        [_request("a", 0, 3), _request("b", 0, 3), _request("c", 0, 3)],
        target_concurrency=5,
        threshold_ratio=0.8,
    )
    output = render_terminal("not-found", result)
    rows = _chart_rows(output, "Concurrency Timeline")
    peak_row = next(index for index, line in enumerate(rows) if line.lstrip().startswith("3 |"))
    threshold_row = next(index for index, line in enumerate(rows) if line.lstrip().startswith("4 |"))

    assert threshold_row < peak_row
    assert "threshold" in rows[threshold_row]


def test_completed_chart_keeps_exact_completed_boundary_level():
    output = render_terminal("perf", _synthetic_renderer_result())
    rows = _chart_rows(output, "Completed Requests")
    end_row = next(line for line in rows if line.lstrip().startswith("107 |"))

    assert "●" in end_row


def test_completed_chart_draws_horizontal_guides_to_steady_markers():
    result = SteadyStateResult(
        status="found",
        total_requests=100,
        successful_requests=100,
        target_concurrency=10,
        threshold_ratio=1,
        threshold_concurrency=10,
        observed_peak=10,
        steady_start_s=20,
        steady_end_s=80,
        steady_duration_s=60,
        completed_at_start=10,
        completed_at_end=80,
        warning=None,
        reason=None,
        timeline=(
            TimelinePoint(time_s=0, running_requests=10, completed_requests=0),
            TimelinePoint(time_s=100, running_requests=0, completed_requests=100),
        ),
    )
    width = 40
    output = render_terminal("guides", result, width=width)
    rows = _chart_rows(output, "Completed Requests")

    for value, time_s in ((10, 20), (80, 80)):
        row = next(line for line in rows if line.lstrip().startswith(f"{value} |"))
        cells = row.split("|", maxsplit=1)[1]
        marker_column = time_to_column(time_s, 100, width)
        assert cells[marker_column] == "●"
        assert all(character != " " for character in cells[:marker_column])


def test_mandatory_levels_take_priority_at_low_chart_height():
    levels = _assign_chart_levels(
        max_value=35,
        height=4,
        mandatory_levels=(35, 34, 0),
        optional_levels=(26, 18, 9),
    )

    assert set(levels.values()) >= {0, 34, 35}
    assert len({row for row, level in levels.items() if level in {0, 34, 35}}) == 3
    assert next(row for row, level in levels.items() if level == 35) < next(
        row for row, level in levels.items() if level == 34
    )


def test_chart_rejects_more_mandatory_levels_than_rows():
    with pytest.raises(ValueError, match="mandatory Y levels"):
        _assign_chart_levels(
            max_value=4,
            height=4,
            mandatory_levels=(0, 1, 2, 3, 4),
            optional_levels=(),
        )


def test_renderer_draws_vertical_steady_boundaries_through_both_charts():
    requests = [
        _request("long", 0, 80),
        _request("steady", 20, 60),
    ]
    result = analyze_steady_state(requests, target_concurrency=2, threshold_ratio=1)

    output = render_terminal("boundaries", result, width=40)
    start_col = time_to_column(20, 80, 40)
    end_col = time_to_column(60, 80, 40)

    for title in ("Concurrency Timeline", "Completed Requests"):
        rows = _chart_rows(output, title)
        assert len(rows) >= 3
        cells = [row.split("|", maxsplit=1)[1] for row in rows]
        assert sum(row[start_col] in "┆┼●" for row in cells) >= 3
        assert sum(row[end_col] in "┆┼●" for row in cells) >= 3


def test_renderer_uses_shared_time_axis_and_multiple_ticks():
    result = analyze_steady_state(
        [_request("long", 0, 80), _request("steady", 20, 60)],
        target_concurrency=2,
        threshold_ratio=1,
    )

    output = render_terminal("axes", result, width=40)
    concurrency = output.split("Concurrency Timeline\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    completed = output.split("Completed Requests\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]
    concurrency_axis = [line.lstrip() for line in concurrency.splitlines() if "┬" in line or "0s" in line]
    completed_axis = [line.lstrip() for line in completed.splitlines() if "┬" in line or "0s" in line]

    assert concurrency_axis == completed_axis
    assert concurrency_axis[0].count("┬") >= 5


def test_renderer_preserves_a_middle_dip_in_thin_step_line():
    result = analyze_steady_state(
        [
            _request("base-1", 0, 80),
            _request("base-2", 0, 80),
            _request("before-dip", 0, 30),
            _request("after-dip", 40, 80),
        ],
        target_concurrency=4,
        threshold_ratio=1,
    )

    output = render_terminal("dip", result, width=40)
    chart = output.split("Concurrency Timeline\n", maxsplit=1)[1].split("\n\n", maxsplit=1)[0]

    assert "█" not in chart
    assert "┐" in chart
    assert "└" in chart
    assert "┘" in chart
    assert "┌" in chart


def test_summary_keeps_nested_time_and_completed_coordinates():
    result = analyze_steady_state(
        [_request("a", 0, 12), _request("b", 1, 11)],
        target_concurrency=2,
    )

    summary = steady_state_summary("perf", result)

    assert summary["steady_state"] == {
        "start": {"time_s": 1, "completed_requests": 0},
        "end": {"time_s": 11, "completed_requests": 1},
        "duration_s": 10,
    }


@pytest.mark.parametrize(
    ("target_concurrency", "threshold_ratio"),
    [(0, 0.95), (1, 0), (1, 1.1)],
)
def test_invalid_analysis_parameters_are_rejected(target_concurrency: int, threshold_ratio: float):
    with pytest.raises(ValueError):
        analyze_steady_state([], target_concurrency, threshold_ratio)
