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
from pathlib import Path
from typing import Literal

DEFAULT_STEADY_STATE_THRESHOLD = 0.95
MIN_STEADY_STATE_WINDOW_S = 10.0
DEFAULT_TIMELINE_WIDTH = 64
DEFAULT_CHART_HEIGHT = 8

SteadyStateStatus = Literal["found", "not_found", "skipped", "unavailable"]


@dataclass(frozen=True)
class RequestTiming:
    """Timing data for one benchmark request."""

    request_id: str
    start_time: float
    end_time: float
    success: bool


@dataclass(frozen=True)
class TimingLoadStats:
    """Counts collected while loading request timing artifacts."""

    records_read: int
    valid_timings: int
    successful_timings: int
    invalid_records: int


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


def time_to_column(time_s: float, duration_s: float, width: int) -> int:
    """Map a benchmark-relative time to a shared zero-based chart column."""

    if width <= 0:
        raise ValueError("timeline width must be greater than zero")
    if duration_s <= 0:
        return 0
    bounded_time = max(0.0, min(time_s, duration_s))
    return round(bounded_time / duration_s * (width - 1))


def _linear_value_to_row(value: int, max_value: int, height: int) -> int:
    bounded_value = max(0, min(value, max_value))
    return (height - 1) - round(bounded_value / max_value * (height - 1))


def _assign_chart_levels(
    *,
    max_value: int,
    height: int,
    mandatory_levels: Sequence[int],
    optional_levels: Sequence[int],
) -> dict[int, int]:
    """Assign important Y values to distinct rows before adding optional ticks."""

    if height <= 0:
        raise ValueError("chart height must be greater than zero")
    if max_value <= 0:
        raise ValueError("chart max value must be greater than zero")

    bounded_mandatory = {max(0, min(level, max_value)) for level in (*mandatory_levels, 0, max_value)}
    mandatory = sorted(bounded_mandatory, reverse=True)
    if len(mandatory) > height:
        raise ValueError(f"{len(mandatory)} mandatory Y levels cannot fit in {height} chart rows")

    levels_by_row: dict[int, int] = {}
    previous_row = -1
    for index, level in enumerate(mandatory):
        rows_remaining = len(mandatory) - index - 1
        first_available_row = previous_row + 1
        last_available_row = height - rows_remaining - 1
        preferred_row = _linear_value_to_row(level, max_value, height)
        row = max(first_available_row, min(preferred_row, last_available_row))
        levels_by_row[row] = level
        previous_row = row

    for level in sorted({max(0, min(value, max_value)) for value in optional_levels}, reverse=True):
        if level in levels_by_row.values():
            continue
        rows_above = [row for row, assigned in levels_by_row.items() if assigned > level]
        rows_below = [row for row, assigned in levels_by_row.items() if assigned < level]
        first_available_row = max(rows_above, default=-1) + 1
        last_available_row = min(rows_below, default=height) - 1
        available_rows = [row for row in range(first_available_row, last_available_row + 1) if row not in levels_by_row]
        if not available_rows:
            continue
        preferred_row = _linear_value_to_row(level, max_value, height)
        row = min(available_rows, key=lambda candidate: (abs(candidate - preferred_row), candidate))
        levels_by_row[row] = level

    return levels_by_row


def _value_to_row(value: int, max_value: int, height: int, levels_by_row: dict[int, int]) -> int:
    """Map a value using piecewise interpolation through the displayed Y levels."""

    bounded_value = max(0, min(value, max_value))
    rows_by_level = {level: row for row, level in levels_by_row.items()}
    if bounded_value in rows_by_level:
        return rows_by_level[bounded_value]

    anchors = sorted(rows_by_level.items())
    for (lower_level, lower_row), (upper_level, upper_row) in zip(anchors, anchors[1:]):
        if lower_level < bounded_value < upper_level:
            ratio = (bounded_value - lower_level) / (upper_level - lower_level)
            return round(lower_row + ratio * (upper_row - lower_row))
    return _linear_value_to_row(bounded_value, max_value, height)


_CONNECTION_CHARACTERS = {
    frozenset({"left"}): "─",
    frozenset({"right"}): "─",
    frozenset({"left", "right"}): "─",
    frozenset({"up"}): "│",
    frozenset({"down"}): "│",
    frozenset({"up", "down"}): "│",
    frozenset({"right", "down"}): "┌",
    frozenset({"left", "down"}): "┐",
    frozenset({"right", "up"}): "└",
    frozenset({"left", "up"}): "┘",
}


def _draw_step_line(
    values: Sequence[int],
    max_value: int,
    height: int,
    levels_by_row: dict[int, int],
) -> list[list[str]]:
    connections = [[set() for _ in values] for _ in range(height)]
    rows = [_value_to_row(value, max_value, height, levels_by_row) for value in values]
    if len(values) == 1:
        connections[rows[0]][0].update(("left", "right"))
    for column in range(1, len(values)):
        previous_row = rows[column - 1]
        current_row = rows[column]
        connections[previous_row][column - 1].add("right")
        connections[previous_row][column].add("left")
        if current_row > previous_row:
            for row in range(previous_row, current_row):
                connections[row][column].add("down")
                connections[row + 1][column].add("up")
        elif current_row < previous_row:
            for row in range(previous_row, current_row, -1):
                connections[row][column].add("up")
                connections[row - 1][column].add("down")

    canvas = [[" " for _ in values] for _ in range(height)]
    for row in range(height):
        for column in range(len(values)):
            cell = frozenset(connections[row][column])
            if cell:
                canvas[row][column] = _CONNECTION_CHARACTERS.get(cell, "┼")
    return canvas


def _draw_horizontal_guide(canvas: list[list[str]], row: int, marker_column: int) -> None:
    for column in range(marker_column + 1):
        character = canvas[row][column]
        canvas[row][column] = "─" if character in (" ", "─") else "┼"


def _draw_vertical_boundary(canvas: list[list[str]], column: int) -> None:
    for row in range(len(canvas)):
        canvas[row][column] = "┆" if canvas[row][column] == " " else "┼"


def _nice_tick_interval(duration_s: float) -> float:
    if duration_s <= 0:
        return 1.0
    raw_interval = duration_s / 6
    magnitude = 10 ** math.floor(math.log10(raw_interval))
    fraction = raw_interval / magnitude
    if fraction <= 1:
        nice_fraction = 1
    elif fraction <= 2:
        nice_fraction = 2
    elif fraction <= 5:
        nice_fraction = 5
    else:
        nice_fraction = 10
    return nice_fraction * magnitude


def _format_tick(time_s: float) -> str:
    if math.isclose(time_s, round(time_s)):
        return f"{round(time_s)}s"
    return f"{time_s:g}s"


def _render_time_axis(duration_s: float, width: int) -> tuple[str, str]:
    interval = _nice_tick_interval(duration_s)
    tick_times = [0.0]
    tick = interval
    while tick < duration_s:
        tick_times.append(tick)
        tick += interval
    if duration_s > 0:
        tick_times.append(duration_s)

    tick_columns = {time_to_column(tick_time, duration_s, width): tick_time for tick_time in tick_times}
    axis = ["─"] * width
    labels = [" "] * width
    occupied = [False] * width
    for column, tick_time in sorted(tick_columns.items()):
        axis[column] = "┬"
        label = _format_tick(tick_time)
        start = min(max(0, column - len(label) // 2), width - len(label))
        if any(occupied[start : start + len(label)]):
            continue
        labels[start : start + len(label)] = label
        occupied[start : start + len(label)] = [True] * len(label)
    return "".join(axis), "".join(labels).rstrip()


def _render_chart(
    *,
    title: str,
    values: Sequence[int],
    duration_s: float,
    max_value: int,
    highlighted_value: int | None = None,
    start_s: float | None = None,
    end_s: float | None = None,
    start_value: int | None = None,
    end_value: int | None = None,
    mandatory_levels: Sequence[int] = (),
    height: int = DEFAULT_CHART_HEIGHT,
) -> list[str]:
    if not values:
        return [title, "  unavailable"]
    max_value = max(max_value, 1)
    mandatory = [*mandatory_levels]
    if highlighted_value is not None:
        mandatory.append(highlighted_value)
    mandatory.extend(value for value in (start_value, end_value) if value is not None)
    optional = [round(max_value * ratio) for ratio in (0.25, 0.5, 0.75)]
    labels = _assign_chart_levels(
        max_value=max_value,
        height=height,
        mandatory_levels=mandatory,
        optional_levels=optional,
    )
    canvas = _draw_step_line(values, max_value, height, labels)
    threshold_row = None
    if highlighted_value is not None:
        threshold_row = _value_to_row(highlighted_value, max_value, height, labels)
        for column, character in enumerate(canvas[threshold_row]):
            canvas[threshold_row][column] = "─" if character in (" ", "─") else "┼"

    for marker_time, marker_value in ((start_s, start_value), (end_s, end_value)):
        if marker_time is None or marker_value is None:
            continue
        column = time_to_column(marker_time, duration_s, len(values))
        row = _value_to_row(marker_value, max_value, height, labels)
        _draw_horizontal_guide(canvas, row, column)

    for marker_time in (start_s, end_s):
        if marker_time is None:
            continue
        column = time_to_column(marker_time, duration_s, len(values))
        _draw_vertical_boundary(canvas, column)

    for marker_time, marker_value in ((start_s, start_value), (end_s, end_value)):
        if marker_time is None or marker_value is None:
            continue
        column = time_to_column(marker_time, duration_s, len(values))
        row = _value_to_row(marker_value, max_value, height, labels)
        canvas[row][column] = "●"

    lines = [title]
    label_width = len(str(max(max_value, highlighted_value or 0)))
    for row in range(height):
        label = str(labels[row]) if row in labels else ""
        suffix = " threshold" if row == threshold_row else ""
        lines.append(f"{label:>{label_width}} |{''.join(canvas[row])}{suffix}")
    axis, tick_labels = _render_time_axis(duration_s, len(values))
    prefix = " " * (label_width + 2)
    lines.extend((f"{prefix}{axis}", f"{prefix}{tick_labels}"))
    return lines


def _group_title(case_name: str, result: SteadyStateResult) -> str:
    if result.status == "found":
        assert result.steady_start_s is not None
        assert result.steady_end_s is not None
        detail = (
            f"peak={result.observed_peak}/{result.target_concurrency} | "
            f"{result.steady_start_s:.2f}s→{result.steady_end_s:.2f}s"
        )
        return f"🟢 [STEADY STATE] {case_name} | FOUND | {detail}"
    if result.status == "not_found":
        return (
            f"🟡 [STEADY STATE] {case_name} | NOT_FOUND | "
            f"peak={result.observed_peak}<threshold={result.threshold_concurrency}"
        )
    if result.status == "unavailable":
        return f"🔴 [STEADY STATE] {case_name} | UNAVAILABLE | timing data unavailable"
    return f"⚪ [STEADY STATE] {case_name} | SKIPPED | {result.reason or 'analysis skipped'}"


def render_terminal(
    case_name: str,
    result: SteadyStateResult,
    width: int = DEFAULT_TIMELINE_WIDTH,
    *,
    timing_stats: TimingLoadStats | None = None,
    timing_directory: str | Path | None = None,
    request_rate: float = 0,
    summary_path: str | Path | None = None,
) -> str:
    """Render the complete steady-state GitHub Actions group without I/O."""

    if width < 20:
        raise ValueError("timeline width must be at least 20 columns")

    ratio_percent = result.threshold_ratio * 100
    lines = [f"::group::{_group_title(case_name, result)}"]
    if timing_directory is not None or timing_stats is not None:
        lines.extend(["", "Timing Data"])
        if timing_directory is not None:
            lines.append(f"  Directory:             {timing_directory}")
        if timing_stats is not None:
            lines.extend(
                [
                    f"  Records read:          {timing_stats.records_read}",
                    f"  Valid timings:         {timing_stats.valid_timings}",
                    f"  Successful timings:    {timing_stats.successful_timings}",
                    f"  Invalid records:       {timing_stats.invalid_records}",
                ]
            )
    lines.extend(
        [
            "",
            "Benchmark",
            f"  Total requests:        {result.total_requests}",
            f"  Target concurrency:    {result.target_concurrency}",
            f"  Request rate:          {request_rate:g}",
            "",
            "Steady State",
            f"  Threshold:             {result.threshold_concurrency} ({ratio_percent:g}%)",
            f"  Observed peak:         {result.observed_peak}",
        ]
    )
    if result.status == "found":
        assert result.steady_start_s is not None
        assert result.steady_end_s is not None
        assert result.steady_duration_s is not None
        assert result.completed_at_start is not None
        assert result.completed_at_end is not None
        lines.extend(
            [
                "",
                "Steady Start",
                f"  Time:                   {result.steady_start_s:.2f}s",
                f"  Completed requests:     {result.completed_at_start}",
                "",
                "Steady End",
                f"  Time:                   {result.steady_end_s:.2f}s",
                f"  Completed requests:     {result.completed_at_end}",
                "",
                f"Steady Duration:          {result.steady_duration_s:.2f}s",
            ]
        )
    if result.reason:
        lines.extend(["", "Reason:", f"  {result.reason}"])
    if result.warning:
        lines.extend(["", f"WARNING: {result.warning}"])

    running, completed, duration_s = _sample_timeline(result, width)
    if running:
        lines.extend(
            [
                "",
                *_render_chart(
                    title="Concurrency Timeline",
                    values=running,
                    duration_s=duration_s,
                    max_value=max(result.target_concurrency, result.observed_peak, 1),
                    highlighted_value=result.threshold_concurrency,
                    start_s=result.steady_start_s,
                    end_s=result.steady_end_s,
                    mandatory_levels=(result.observed_peak,),
                ),
                "",
                *_render_chart(
                    title="Completed Requests",
                    values=completed,
                    duration_s=duration_s,
                    max_value=max(result.successful_requests, 1),
                    start_s=result.steady_start_s,
                    end_s=result.steady_end_s,
                    start_value=result.completed_at_start,
                    end_value=result.completed_at_end,
                ),
            ]
        )
        if result.status == "found":
            lines.extend(
                [
                    "",
                    f"steady start: {result.steady_start_s:.2f}s / completed={result.completed_at_start}",
                    f"steady end:   {result.steady_end_s:.2f}s / completed={result.completed_at_end}",
                ]
            )
    if summary_path is not None:
        lines.extend(["", "Summary:", f"  {summary_path}"])
    lines.append("::endgroup::")
    return "\n".join(lines)
