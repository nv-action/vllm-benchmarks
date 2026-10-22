#!/usr/bin/env python3
"""Normalize the pinned AccLibBenchmark protobuf cases from Google Benchmark JSON."""

from __future__ import annotations

import json
import math
import re
import statistics
import sys
from pathlib import Path

TIME_TO_NS = {"ns": 1.0, "us": 1_000.0, "ms": 1_000_000.0, "s": 1_000_000_000.0}
REPETITIONS = 5
GROUPS = (
    "序列化 / Scalar（非 packed）",
    "反序列化 / Scalar（非 packed）",
    "序列化 / Repeated（packed）",
    "反序列化 / Repeated（packed）",
    "序列化 / String 与 Bytes",
    "反序列化 / String 与 Bytes",
)
GROUP_COUNTS = (40, 40, 32, 32, 12, 12)


def group_for_case(name: str) -> str:
    match = re.fullmatch(
        r"BM_(Serialize|Deserialize)_(Scalar|Repeated|String|Bytes)"
        r"(?:_[A-Za-z0-9]+)?/(10|100|1000|10000)",
        name,
    )
    if not match:
        raise ValueError(f"unrecognized original benchmark case: {name}")
    operation, kind, _size = match.groups()
    operation_index = 0 if operation == "Serialize" else 1
    kind_index = {"Scalar": 0, "Repeated": 1, "String": 2, "Bytes": 2}[kind]
    return GROUPS[kind_index * 2 + operation_index]


def normalize(source: Path, cases_file: Path) -> dict:
    expected = set(cases_file.read_text(encoding="utf-8").splitlines())
    if len(expected) != 168:
        raise ValueError(f"expected 168 distinct cases, found {len(expected)}")
    groups = {name: group_for_case(name) for name in expected}
    counts = tuple(sum(group == label for group in groups.values()) for label in GROUPS)
    if counts != GROUP_COUNTS:
        raise ValueError(f"original benchmark case groups differ from the fixed suite: {counts}")

    payload = json.loads(source.read_text(encoding="utf-8"))
    samples: dict[str, list[float]] = {name: [] for name in expected}
    for row in payload.get("benchmarks", []):
        if row.get("run_type", "iteration") != "iteration":
            continue
        name = row.get("run_name", row.get("name", ""))
        name = name.split("/repeats:", 1)[0]
        if name not in samples:
            raise ValueError(f"unexpected benchmark case: {name}")
        if row.get("error_occurred"):
            raise ValueError(f"benchmark failed: {name}: {row.get('error_message')}")
        unit = row.get("time_unit")
        if unit not in TIME_TO_NS:
            raise ValueError(f"unsupported time unit for {name}: {unit}")
        value = row.get("cpu_time")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"missing CPU time for {name}")
        value_ns = float(value) * TIME_TO_NS[unit]
        if not math.isfinite(value_ns) or value_ns <= 0:
            raise ValueError(f"invalid CPU time for {name}: {value_ns}")
        samples[name].append(value_ns)

    incomplete = {name: len(values) for name, values in samples.items() if len(values) != REPETITIONS}
    if incomplete:
        raise ValueError(f"cases with missing or extra repetitions: {incomplete}")

    return {
        "source": "AccLibBenchmark/protobuf-benchmark",
        "revision": "01d083c6633babb2fb1307d93f596f10dda36778",
        "parameters": {"min_time": "1s", "repetitions": REPETITIONS},
        "results": {
            name: {
                "source_name": f"{name}/cpu_time_median",
                "group": groups[name],
                "value": statistics.median(values),
                "unit": "ns/op",
                "direction": "lower_is_better",
                "sample_count": len(values),
            }
            for name, values in sorted(
                samples.items(), key=lambda item: (GROUPS.index(groups[item[0]]), item[0])
            )
        },
    }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: parse_google_benchmark.py INPUT_JSON CASES_FILE OUTPUT_JSON")
    try:
        result = normalize(Path(sys.argv[1]), Path(sys.argv[2]))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"[protobuf-parse] ERROR: {exc}", file=sys.stderr)
        return 1
    Path(sys.argv[3]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(f"[protobuf-parse] normalized {len(result['results'])} original C++ cases")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
