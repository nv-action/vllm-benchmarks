#!/usr/bin/env python3
"""Normalize Sonic scenarios from sonic-cpp's official Google Benchmark JSON."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Google Benchmark reports cpu_time in a scaled unit; normalize to nanoseconds
# so every scenario shares a single comparable unit.
TIME_UNIT_TO_NS = {
    "ns": 1.0,
    "us": 1_000.0,
    "ms": 1_000_000.0,
    "s": 1_000_000_000.0,
}


def load_official_benchmark(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read official benchmark JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("official benchmark JSON root must be an object")
    return payload


def select_sonic_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise RuntimeError("official benchmark JSON has no benchmark entries")
    selected = []
    for entry in benchmarks:
        if not isinstance(entry, dict):
            raise TypeError("official benchmark entry is not an object")
        if entry.get("run_type") == "aggregate":
            continue
        name = scenario_name(entry)
        if "Sonic" not in name:
            raise RuntimeError(f"unexpected non-Sonic benchmark scenario: {name}")
        selected.append(entry)
    if not selected:
        raise RuntimeError("official benchmark JSON has no Sonic measurement rows")
    return selected


def scenario_name(entry: dict[str, Any]) -> str:
    name = entry.get("run_name")
    if isinstance(name, str) and name:
        return name
    display = entry.get("name")
    if not isinstance(display, str) or not display:
        raise RuntimeError("official benchmark entry is missing a scenario name")
    return display


def to_nanoseconds(entry: dict[str, Any], name: str) -> tuple[float, float, str]:
    raw_value = entry.get("cpu_time")
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise TypeError(f"scenario {name} has no numeric cpu_time")
    raw_unit = entry.get("time_unit")
    if raw_unit not in TIME_UNIT_TO_NS:
        raise RuntimeError(f"scenario {name} has unsupported time_unit: {raw_unit}")
    value = float(raw_value) * TIME_UNIT_TO_NS[raw_unit]
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"scenario {name} has an invalid cpu_time value")
    return float(raw_value), value, raw_unit


def normalize_results(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for entry in select_sonic_runs(payload):
        name = scenario_name(entry)
        if name in results:
            raise RuntimeError(
                f"official benchmark produced duplicate scenario: {name}"
            )
        raw_value, value, raw_unit = to_nanoseconds(entry, name)
        results[name] = {
            "source_name": name,
            "source_field": "cpu_time",
            "raw_value": raw_value,
            "raw_unit": raw_unit,
            "value": value,
            "unit": "ns",
        }
    if not results:
        raise RuntimeError("official benchmark JSON produced no scenarios")
    return results


def build_runtime_context(payload: dict[str, Any]) -> dict[str, Any]:
    context = payload.get("context")
    if not isinstance(context, dict):
        return {}
    runtime: dict[str, Any] = {}
    for key in (
        "num_cpus",
        "mhz_per_cpu",
        "cpu_scaling_enabled",
        "caches",
        "load_avg",
        "library_version",
        "library_build_type",
    ):
        if key in context:
            runtime[key] = context[key]
    return runtime


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: parse_benchmark.py OFFICIAL_BENCHMARK_JSON NORMALIZED_OUTPUT",
            file=sys.stderr,
        )
        return 1
    official_path, normalized_output = Path(sys.argv[1]), Path(sys.argv[2])

    try:
        payload = load_official_benchmark(official_path)
        results = normalize_results(payload)
    except (RuntimeError, TypeError, ValueError) as exc:
        print(f"[sonic-cpp-parse] ERROR: {exc}", file=sys.stderr)
        return 1

    version = os.environ["SOFTWARE_VERSION"]
    architecture = os.environ["EXPECTED_ARCH"]
    min_time = os.environ.get("BENCHMARK_MIN_TIME", "3s")
    normalized = {
        "benchmark": "sonic_cpp_official_benchmark",
        "software": "sonic-cpp",
        "version": version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "command": [
                "build/benchmark/bench",
                "--benchmark_out_format=json",
                "--benchmark_filter=Sonic",
                "--benchmark_min_time=" + min_time,
            ],
            "benchmark_filter": "Sonic",
            "benchmark_min_time": min_time,
            "official_entry": "benchmark/main.cpp",
        },
        "metric_contract": {
            "scope": "cpu_time of each selected Sonic benchmark scenario",
            "source_field": "cpu_time",
            "normalized_unit": "ns",
            "direction": "lower_is_better",
        },
        "runtime_context": build_runtime_context(payload),
        "results": results,
    }

    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[sonic-cpp-parse] normalized {len(results)} official scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
