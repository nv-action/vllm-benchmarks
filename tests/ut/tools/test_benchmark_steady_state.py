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
    analyze_steady_state,
    render_terminal,
    steady_state_summary,
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

    output = render_terminal("perf", result, width=24)

    assert "::group::Steady State Analysis: perf" in output
    assert "Running Requests" in output
    assert "Completed Requests" in output
    assert "3.00s" in output
    assert "completed=0" in output
    assert "20.00s" in output
    assert "completed=1" in output


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
