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

import json
import sqlite3
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest

from tools.aisbench_perf_data import AisbenchTimingAdapter, TimingDataUnavailable


def _write_numpy_store(path: Path, arrays: list[np.ndarray]) -> None:
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE numpy_store (id INTEGER PRIMARY KEY AUTOINCREMENT, arr_blob BLOB NOT NULL)")
    for array in arrays:
        buffer = BytesIO()
        np.save(buffer, array)
        connection.execute("INSERT INTO numpy_store (arr_blob) VALUES (?)", (sqlite3.Binary(buffer.getvalue()),))
    connection.commit()
    connection.close()


def _write_jsonl(path: Path, records: list[object]) -> None:
    path.write_text("".join(f"{json.dumps(record)}\n" for record in records), encoding="utf-8")


def test_adapter_reads_inline_and_database_backed_time_points(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    caplog.set_level("INFO", logger="tools.aisbench_perf_data")
    _write_numpy_store(tmp_path / "db_data" / "worker.db", [np.array([20.0, 21.0, 25.0])])
    _write_jsonl(
        tmp_path / "gsm8k_details.jsonl",
        [
            {"id": 1, "success": True, "time_points": [10.0, 11.0, 12.0]},
            {
                "id": 2,
                "success": True,
                "time_points": {"__db_ref__": 1},
                "db_name": "worker.db",
            },
            {"id": 3, "success": False, "time_points": [30.0, 31.0]},
            {"id": 4, "success": True, "time_points": [40.0]},
        ],
    )

    load_result = AisbenchTimingAdapter(tmp_path, "gsm8k").load_request_timings()
    timings = load_result.timings

    assert [(timing.request_id, timing.start_time, timing.end_time, timing.success) for timing in timings] == [
        ("1", 10.0, 12.0, True),
        ("2", 20.0, 25.0, True),
        ("3", 30.0, 31.0, False),
    ]
    assert "fewer than two entries" in caplog.text
    assert "AISBench Timing Data" not in caplog.text
    assert load_result.stats.records_read == 4
    assert load_result.stats.valid_timings == 3
    assert load_result.stats.successful_timings == 2
    assert load_result.stats.invalid_records == 1


def test_adapter_reuses_one_read_only_connection_per_database(tmp_path: Path):
    _write_numpy_store(
        tmp_path / "db_data" / "worker.db",
        [np.array([1.0, 2.0]), np.array([3.0, 4.0])],
    )
    _write_jsonl(
        tmp_path / "dataset_details.jsonl",
        [
            {
                "id": index,
                "success": True,
                "time_points": {"__db_ref__": index},
                "db_name": "worker.db",
            }
            for index in (1, 2)
        ],
    )

    with patch("tools.aisbench_perf_data.sqlite3.connect", wraps=sqlite3.connect) as connect:
        load_result = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()

    assert len(load_result.timings) == 2
    assert connect.call_count == 1


def test_adapter_resolves_relative_result_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    result_dir = tmp_path / "outputs" / "performances" / "model"
    _write_numpy_store(result_dir / "db_data" / "worker.db", [np.array([1.0, 2.0])])
    _write_jsonl(
        result_dir / "dataset_details.jsonl",
        [
            {
                "id": 1,
                "success": True,
                "time_points": {"__db_ref__": 1},
                "db_name": "worker.db",
            }
        ],
    )
    monkeypatch.chdir(tmp_path)

    adapter = AisbenchTimingAdapter(Path("outputs/performances/model"), "dataset")
    load_result = adapter.load_request_timings()

    assert adapter.result_dir == result_dir
    assert [(timing.start_time, timing.end_time) for timing in load_result.timings] == [(1.0, 2.0)]


def test_adapter_uses_only_fallback_details_file(tmp_path: Path):
    _write_jsonl(
        tmp_path / "different_details.jsonl",
        [{"id": 1, "success": True, "time_points": [1.0, 2.0]}],
    )

    load_result = AisbenchTimingAdapter(tmp_path, "expected").load_request_timings()

    assert len(load_result.timings) == 1


def test_adapter_reports_missing_details_file(tmp_path: Path):
    with pytest.raises(TimingDataUnavailable, match=r"no \*_details.jsonl"):
        AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()


def test_adapter_refuses_to_guess_between_multiple_details_files(tmp_path: Path):
    _write_jsonl(tmp_path / "a_details.jsonl", [])
    _write_jsonl(tmp_path / "b_details.jsonl", [])

    with pytest.raises(TimingDataUnavailable, match="multiple"):
        AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()


@pytest.mark.parametrize(
    "record",
    [
        {"id": 1, "success": True, "time_points": [float("inf"), 2.0]},
        {"id": 1, "success": True, "time_points": [2.0, 1.0]},
        {"id": 1, "success": True, "time_points": {"__db_ref__": 1}},
    ],
)
def test_adapter_skips_invalid_timing_records(tmp_path: Path, record: dict[str, object]):
    _write_jsonl(tmp_path / "dataset_details.jsonl", [record])

    assert AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings().timings == []


def test_adapter_skips_one_invalid_record_and_keeps_remaining_records(tmp_path: Path):
    _write_jsonl(
        tmp_path / "dataset_details.jsonl",
        [
            {"id": "bad", "success": True, "time_points": [2.0, 1.0]},
            {"id": "good", "success": True, "time_points": [3.0, 4.0]},
        ],
    )

    timings = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings().timings

    assert [(timing.request_id, timing.start_time, timing.end_time, timing.success) for timing in timings] == [
        ("good", 3.0, 4.0, True)
    ]


def test_adapter_skips_malformed_json_and_keeps_remaining_records(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    (tmp_path / "dataset_details.jsonl").write_text(
        '{"id": "bad"\n' + json.dumps({"id": "good", "success": True, "time_points": [3.0, 4.0]}) + "\n",
        encoding="utf-8",
    )

    timings = AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings().timings

    assert [timing.request_id for timing in timings] == ["good"]
    assert "invalid JSON" in caplog.text


def test_adapter_reports_missing_database_once_as_artifact_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    _write_jsonl(
        tmp_path / "dataset_details.jsonl",
        [
            {
                "id": index,
                "success": True,
                "time_points": {"__db_ref__": index},
                "db_name": "missing.db",
            }
            for index in (1, 2)
        ],
    )

    with pytest.raises(TimingDataUnavailable, match="database does not exist"):
        AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()

    assert "Skipping invalid AISBench timing record" not in caplog.text


def test_adapter_reports_invalid_sqlite_as_artifact_failure(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    db_path = tmp_path / "db_data" / "worker.db"
    db_path.parent.mkdir()
    db_path.write_text("not a sqlite database", encoding="utf-8")
    _write_jsonl(
        tmp_path / "dataset_details.jsonl",
        [
            {
                "id": 1,
                "success": True,
                "time_points": {"__db_ref__": 1},
                "db_name": "worker.db",
            }
        ],
    )

    with pytest.raises(TimingDataUnavailable, match="cannot read AISBench timing database"):
        AisbenchTimingAdapter(tmp_path, "dataset").load_request_timings()

    assert "Skipping invalid AISBench timing record" not in caplog.text
