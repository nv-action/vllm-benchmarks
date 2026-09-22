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

"""Pure steady-state analysis and rendering for benchmark request timings."""

from __future__ import annotations

import math
from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

DEFAULT_STEADY_STATE_THRESHOLD = 0.95
MIN_STEADY_STATE_WINDOW_S = 10.0
DEFAULT_TIMELINE_WIDTH = 64

SteadyStateStatus = Literal["found", "not_found", "skipped", "unavailable"]


@dataclass(frozen=True)
class RequestTiming:
    """Timing data for one benchmark request."""

    request_id: str
    start_time: float
    end_time: float
    success: bool


@dataclass(frozen=True)
class TimelinePoint:
    """Exact request counters after one start or end event."""

    time_s: float
    running_requests: int
    completed_requests: int


@dataclass(frozen=True)
class SteadyStateResult:
    """Steady-state summary plus the event-exact timeline used to render it."""

    status: SteadyStateStatus
    total_requests: int
    successful_requests: int
    target_concurrency: int
    threshold_ratio: float
    threshold_concurrency: int
    observed_peak: int
    steady_start_s: float | None
    steady_end_s: float | None
    steady_duration_s: float | None
    completed_at_start: int | None
    completed_at_end: int | None
    warning: str | None
    reason: str | None
    timeline: tuple[TimelinePoint, ...] = ()


@dataclass(frozen=True)
class _Event:
    time: float
    kind: Literal["end", "instant", "start"]


def _empty_result(
    *,
    status: SteadyStateStatus,
    total_requests: int,
    target_concurrency: int,
    threshold_ratio: float,
    reason: str,
) -> SteadyStateResult:
    return SteadyStateResult(
        status=status,
        total_requests=total_requests,
        successful_requests=0,
        target_concurrency=target_concurrency,
        threshold_ratio=threshold_ratio,
        threshold_concurrency=math.ceil(target_concurrency * threshold_ratio),
        observed_peak=0,
        steady_start_s=None,
        steady_end_s=None,
        steady_duration_s=None,
        completed_at_start=None,
        completed_at_end=None,
        warning=None,
        reason=reason,
    )


def unavailable_steady_state(
    *,
    target_concurrency: int,
    reason: str,
    threshold_ratio: float = DEFAULT_STEADY_STATE_THRESHOLD,
) -> SteadyStateResult:
    """Build an unavailable result when request timing artifacts cannot be read."""

    _validate_parameters(target_concurrency, threshold_ratio)
    return _empty_result(
        status="unavailable",
        total_requests=0,
        target_concurrency=target_concurrency,
        threshold_ratio=threshold_ratio,
        reason=reason,
    )


def _validate_parameters(target_concurrency: int, threshold_ratio: float) -> None:
    if target_concurrency <= 0:
        raise ValueError("target_concurrency must be greater than zero")
    if not 0 < threshold_ratio <= 1:
        raise ValueError("threshold_ratio must be in the interval (0, 1]")


def analyze_steady_state(
    requests: Sequence[RequestTiming],
    target_concurrency: int,
    threshold_ratio: float = DEFAULT_STEADY_STATE_THRESHOLD,
    request_rate: float = 0,
) -> SteadyStateResult:
    """Reconstruct request concurrency and locate the broad steady-state window.

    The window begins at the first upward threshold crossing and ends at the
    last downward crossing. End events sort before start events at equal
    timestamps so a hand-off cannot create a false concurrency peak.
    """

    _validate_parameters(target_concurrency, threshold_ratio)
    total_requests = len(requests)
    if request_rate > 0:
        return _empty_result(
            status="skipped",
            total_requests=total_requests,
            target_concurrency=target_concurrency,
            threshold_ratio=threshold_ratio,
            reason="rate-controlled workload",
        )

    successful_requests = [request for request in requests if request.success]
    if not successful_requests:
        return _empty_result(
            status="unavailable",
            total_requests=total_requests,
            target_concurrency=target_concurrency,
            threshold_ratio=threshold_ratio,
            reason="no successful request timing data",
        )

    for request in successful_requests:
        if not math.isfinite(request.start_time) or not math.isfinite(request.end_time):
            raise ValueError(f"request {request.request_id!r} has a non-finite timestamp")
        if request.end_time < request.start_time:
            raise ValueError(f"request {request.request_id!r} ends before it starts")

    benchmark_zero = min(request.start_time for request in successful_requests)
    completed_times = sorted(request.end_time for request in successful_requests)
    events: list[_Event] = []
    for request in successful_requests:
        if request.start_time == request.end_time:
            events.append(_Event(request.end_time, "instant"))
        else:
            events.extend((_Event(request.start_time, "start"), _Event(request.end_time, "end")))
    event_order = {"end": 0, "instant": 1, "start": 2}
    events.sort(key=lambda event: (event.time, event_order[event.kind]))

    threshold_concurrency = math.ceil(target_concurrency * threshold_ratio)
    running = 0
    completed = 0
    observed_peak = 0
    steady_start_abs: float | None = None
    last_down_crossing: float | None = None
    timeline: list[TimelinePoint] = []

    for event in events:
        before = running
        if event.kind == "end":
            running -= 1
            completed += 1
        elif event.kind == "instant":
            completed += 1
        else:
            running += 1
            observed_peak = max(observed_peak, running)

        if steady_start_abs is None and before < threshold_concurrency <= running:
            steady_start_abs = event.time
        if before >= threshold_concurrency > running:
            last_down_crossing = event.time

        timeline.append(
            TimelinePoint(
                time_s=event.time - benchmark_zero,
                running_requests=running,
                completed_requests=completed,
            )
        )

    if steady_start_abs is None or last_down_crossing is None:
        return SteadyStateResult(
            status="not_found",
            total_requests=total_requests,
            successful_requests=len(successful_requests),
            target_concurrency=target_concurrency,
            threshold_ratio=threshold_ratio,
            threshold_concurrency=threshold_concurrency,
            observed_peak=observed_peak,
            steady_start_s=None,
            steady_end_s=None,
            steady_duration_s=None,
            completed_at_start=None,
            completed_at_end=None,
            warning=None,
            reason="observed concurrency never reached the steady-state threshold",
            timeline=tuple(timeline),
        )

    steady_start_s = steady_start_abs - benchmark_zero
    steady_end_s = last_down_crossing - benchmark_zero
    steady_duration_s = steady_end_s - steady_start_s
    total_duration_s = max(completed_times) - benchmark_zero
    min_window_s = max(MIN_STEADY_STATE_WINDOW_S, total_duration_s * 0.1)
    warning = None
    if steady_duration_s < min_window_s:
        warning = (
            f"Steady-state window is only {steady_duration_s:.2f}s and may not be representative "
            f"(recommended minimum: {min_window_s:.2f}s)."
        )

    return SteadyStateResult(
        status="found",
        total_requests=total_requests,
        successful_requests=len(successful_requests),
        target_concurrency=target_concurrency,
        threshold_ratio=threshold_ratio,
        threshold_concurrency=threshold_concurrency,
        observed_peak=observed_peak,
        steady_start_s=steady_start_s,
        steady_end_s=steady_end_s,
        steady_duration_s=steady_duration_s,
        completed_at_start=bisect_right(completed_times, steady_start_abs),
        completed_at_end=bisect_right(completed_times, last_down_crossing),
        warning=warning,
        reason=None,
        timeline=tuple(timeline),
    )


def steady_state_summary(case_name: str, result: SteadyStateResult) -> dict[str, object]:
    """Convert a result to the stable machine-readable summary schema."""

    steady_state = None
    if result.status == "found":
        steady_state = {
            "start": {
                "time_s": result.steady_start_s,
                "completed_requests": result.completed_at_start,
            },
            "end": {
                "time_s": result.steady_end_s,
                "completed_requests": result.completed_at_end,
            },
            "duration_s": result.steady_duration_s,
        }
    return {
        "case_name": case_name,
        "status": result.status,
        "total_requests": result.total_requests,
        "successful_requests": result.successful_requests,
        "target_concurrency": result.target_concurrency,
        "threshold_ratio": result.threshold_ratio,
        "threshold_concurrency": result.threshold_concurrency,
        "observed_peak": result.observed_peak,
        "steady_state": steady_state,
        "warning": result.warning,
        "reason": result.reason,
    }


def _sample_timeline(result: SteadyStateResult, width: int) -> tuple[list[int], list[int], float]:
    if not result.timeline:
        return [], [], 0.0
    duration_s = result.timeline[-1].time_s
    if duration_s <= 0:
        point = result.timeline[-1]
        return [point.running_requests], [point.completed_requests], 0.0

    running: list[int] = []
    completed: list[int] = []
    point_index = 0
    for column in range(width):
        sample_time = duration_s * column / (width - 1)
        while point_index + 1 < len(result.timeline) and result.timeline[point_index + 1].time_s <= sample_time:
            point_index += 1
        point = result.timeline[point_index]
        running.append(point.running_requests)
        completed.append(point.completed_requests)
    return running, completed, duration_s


def _chart_levels(max_value: int, highlighted_value: int | None = None) -> list[int]:
    candidates = {0, max_value}
    candidates.update(round(max_value * ratio) for ratio in (0.25, 0.5, 0.75))
    if highlighted_value is not None:
        candidates.add(highlighted_value)
    return sorted(candidates, reverse=True)


def _render_chart(
    *,
    title: str,
    values: Sequence[int],
    duration_s: float,
    max_value: int,
    highlighted_value: int | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
) -> list[str]:
    if not values:
        return [title, "  unavailable"]
    lines = [title]
    label_width = len(str(max_value))
    for level in _chart_levels(max_value, highlighted_value):
        if level == 0:
            cells = "─" * len(values)
        else:
            background = "─" if level == highlighted_value else " "
            cells = "".join("█" if value >= level else background for value in values)
        suffix = " threshold" if level == highlighted_value else ""
        lines.append(f"{level:>{label_width}} |{cells}|{suffix}")
    lines.append(" " * (label_width + 1) + "+" + "─" * len(values) + "+")
    lines.append(f"{' ' * (label_width + 2)}0s{' ' * max(1, len(values) - 9)}{duration_s:>7.2f}s")

    markers = [" "] * len(values)
    for marker_time, marker in ((start_s, "S"), (end_s, "E")):
        if marker_time is None:
            continue
        column = 0 if duration_s <= 0 else round(marker_time / duration_s * (len(values) - 1))
        markers[max(0, min(column, len(markers) - 1))] = marker
    if "S" in markers or "E" in markers:
        lines.append(" " * (label_width + 2) + "".join(markers))
    return lines


def render_terminal(case_name: str, result: SteadyStateResult, width: int = DEFAULT_TIMELINE_WIDTH) -> str:
    """Render a compact GitHub Actions log group without performing I/O."""

    if width < 20:
        raise ValueError("timeline width must be at least 20 columns")

    ratio_percent = result.threshold_ratio * 100
    lines = [
        f"::group::Steady State Analysis: {case_name}",
        f"Status: {result.status.upper()}",
        "",
        f"Target concurrency:       {result.target_concurrency}",
        f"Threshold:                {result.threshold_concurrency} ({ratio_percent:g}%)",
        f"Observed peak:            {result.observed_peak}",
    ]
    if result.status == "found":
        assert result.steady_start_s is not None
        assert result.steady_end_s is not None
        assert result.steady_duration_s is not None
        assert result.completed_at_start is not None
        assert result.completed_at_end is not None
        lines.extend(
            [
                "",
                "Steady Start:",
                f"  Time:                   {result.steady_start_s:.2f}s",
                f"  Completed requests:     {result.completed_at_start}",
                "",
                "Steady End:",
                f"  Time:                   {result.steady_end_s:.2f}s",
                f"  Completed requests:     {result.completed_at_end}",
                "",
                f"Steady Duration:          {result.steady_duration_s:.2f}s",
            ]
        )
    if result.reason:
        lines.extend(["", f"Reason: {result.reason}"])
    if result.warning:
        lines.extend(["", f"WARNING: {result.warning}"])

    running, completed, duration_s = _sample_timeline(result, width)
    if running:
        lines.extend(
            [
                "",
                *_render_chart(
                    title="Running Requests",
                    values=running,
                    duration_s=duration_s,
                    max_value=max(result.target_concurrency, result.observed_peak, 1),
                    highlighted_value=result.threshold_concurrency,
                    start_s=result.steady_start_s,
                    end_s=result.steady_end_s,
                ),
                "",
                *_render_chart(
                    title="Completed Requests",
                    values=completed,
                    duration_s=duration_s,
                    max_value=max(result.successful_requests, 1),
                    start_s=result.steady_start_s,
                    end_s=result.steady_end_s,
                ),
            ]
        )
        if result.status == "found":
            lines.extend(
                [
                    "",
                    f"S steady start: {result.steady_start_s:.2f}s / completed={result.completed_at_start}",
                    f"E steady end:   {result.steady_end_s:.2f}s / completed={result.completed_at_end}",
                ]
            )
    lines.append("::endgroup::")
    return "\n".join(lines)
