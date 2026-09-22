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

"""Minimal request-timing reader for AISBench performance artifacts."""

from __future__ import annotations

import json
import logging
import math
import sqlite3
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import numpy as np

from tools.benchmark_steady_state import RequestTiming, TimingLoadStats

logger = logging.getLogger(__name__)


class TimingDataUnavailable(RuntimeError):
    """Raised when no unambiguous AISBench timing artifact can be selected."""


class InvalidTimingRecord(ValueError):
    """Raised when one AISBench request record cannot produce a timing."""


@dataclass(frozen=True)
class TimingLoadResult:
    """Valid request timings and the statistics collected while loading them."""

    timings: list[RequestTiming]
    stats: TimingLoadStats


class AisbenchTimingAdapter:
    """Convert AISBench detail records into benchmark-agnostic timings."""

    def __init__(self, result_dir: str | Path, dataset_type: str) -> None:
        self.result_dir = Path(result_dir).resolve()
        self.dataset_type = dataset_type
        self._connections: dict[str, sqlite3.Connection] = {}

    def _find_details_file(self) -> Path:
        expected = self.result_dir / f"{self.dataset_type}_details.jsonl"
        if expected.is_file():
            return expected

        candidates = sorted(self.result_dir.glob("*_details.jsonl"))
        if not candidates:
            raise TimingDataUnavailable(f"no *_details.jsonl file found in {self.result_dir}")
        if len(candidates) > 1:
            names = ", ".join(path.name for path in candidates)
            raise TimingDataUnavailable(f"multiple *_details.jsonl files found ({names}); refusing to guess")
        return candidates[0]

    def _connection(self, db_name: str) -> sqlite3.Connection:
        if Path(db_name).name != db_name:
            raise InvalidTimingRecord(f"invalid AISBench database name: {db_name!r}")
        if db_name not in self._connections:
            db_path = (self.result_dir / "db_data" / db_name).resolve()
            if not db_path.is_file():
                raise TimingDataUnavailable(f"AISBench timing database does not exist: {db_path}")
            try:
                self._connections[db_name] = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            except sqlite3.Error as exc:
                raise TimingDataUnavailable(f"cannot open AISBench timing database {db_path}: {exc}") from exc
        return self._connections[db_name]

    def _resolve_time_points(self, record: dict[str, object]) -> list[float]:
        value = record.get("time_points")
        if isinstance(value, list):
            try:
                return [float(point) for point in value]
            except (TypeError, ValueError, OverflowError) as exc:
                raise InvalidTimingRecord(f"time_points contains a non-numeric value: {exc}") from exc
        if not isinstance(value, dict) or "__db_ref__" not in value:
            raise InvalidTimingRecord("time_points is neither a list nor an AISBench database reference")

        db_name = record.get("db_name")
        if not isinstance(db_name, str) or not db_name:
            raise InvalidTimingRecord("database-backed time_points has no db_name")
        try:
            row = (
                self._connection(db_name)
                .execute(
                    "SELECT arr_blob FROM numpy_store WHERE id = ?",
                    (value["__db_ref__"],),
                )
                .fetchone()
            )
        except sqlite3.Error as exc:
            db_path = (self.result_dir / "db_data" / db_name).resolve()
            raise TimingDataUnavailable(f"cannot read AISBench timing database {db_path}: {exc}") from exc
        if row is None:
            raise InvalidTimingRecord(f"numpy_store row {value['__db_ref__']!r} does not exist in {db_name}")
        try:
            array = np.load(BytesIO(row[0]), allow_pickle=False)
            return [float(point) for point in np.asarray(array).reshape(-1)]
        except (TypeError, ValueError, OSError) as exc:
            raise InvalidTimingRecord(
                f"numpy_store row {value['__db_ref__']!r} in {db_name} is not a valid NumPy array"
            ) from exc

    def load_request_timings(self) -> TimingLoadResult:
        """Read valid records, warning and skipping malformed request entries."""

        details_file = self._find_details_file()
        timings: list[RequestTiming] = []
        records_read = 0
        invalid_records = 0
        try:
            try:
                with details_file.open(encoding="utf-8") as file:
                    for line_number, line in enumerate(file, start=1):
                        if not line.strip():
                            continue
                        records_read += 1
                        try:
                            try:
                                record = json.loads(line)
                            except json.JSONDecodeError as exc:
                                raise InvalidTimingRecord(f"invalid JSON: {exc}") from exc
                            if not isinstance(record, dict):
                                raise InvalidTimingRecord("detail record is not a JSON object")
                            time_points = self._resolve_time_points(record)
                            if len(time_points) < 2:
                                raise InvalidTimingRecord("time_points contains fewer than two entries")
                            start_time = time_points[0]
                            end_time = time_points[-1]
                            if not math.isfinite(start_time) or not math.isfinite(end_time):
                                raise InvalidTimingRecord("time_points contains a non-finite endpoint")
                            if end_time < start_time:
                                raise InvalidTimingRecord("request end time precedes its start time")
                            request_id = str(record.get("uuid", record.get("id", line_number)))
                            timings.append(
                                RequestTiming(
                                    request_id=request_id,
                                    start_time=start_time,
                                    end_time=end_time,
                                    success=record.get("success") is True,
                                )
                            )
                        except InvalidTimingRecord as exc:
                            invalid_records += 1
                            logger.warning(
                                "Skipping invalid AISBench timing record %s:%d: %s",
                                details_file,
                                line_number,
                                exc,
                            )
            except (OSError, UnicodeError) as exc:
                raise TimingDataUnavailable(f"cannot read AISBench timing details {details_file}: {exc}") from exc
        finally:
            for connection in self._connections.values():
                connection.close()
            self._connections.clear()
        return TimingLoadResult(
            timings=timings,
            stats=TimingLoadStats(
                records_read=records_read,
                valid_timings=len(timings),
                successful_timings=sum(timing.success for timing in timings),
                invalid_records=invalid_records,
            ),
        )
