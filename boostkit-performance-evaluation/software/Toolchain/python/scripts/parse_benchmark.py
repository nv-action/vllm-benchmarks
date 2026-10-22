#!/usr/bin/env python3
"""Normalize pyperformance's official pyperf JSON into per-benchmark metrics.

pyperformance (the official Python performance suite) emits pyperf JSON. This
script extracts one median value per official benchmark, preserving its name
verbatim.
The median reproduces pyperf's own median: statistics.median over every
recorded run value, with warmup values excluded.
"""

from __future__ import annotations

import json
import math
import os
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# pyperf stores values in the unit recorded by each benchmark; normalize to
# seconds so every benchmark shares a single comparable unit.
UNIT_TO_SECONDS = {
    "second": 1.0,
    "millisecond": 1e-3,
    "microsecond": 1e-6,
    "nanosecond": 1e-9,
}

# Metadata keys recorded by pyperf/pyperformance that describe the runtime.
RUNTIME_CONTEXT_KEYS = (
    "performance_version",
    "python_executable",
    "python_implementation",
    "python_version",
    "python_cflags",
    "platform",
    "cpu_count",
    "cpu_model_name",
    "hostname",
    "load_avg_1min",
)


def load_official_benchmark(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read official pyperformance JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("official pyperformance JSON root must be an object")
    version = payload.get("version")
    if not isinstance(version, str) or not version.startswith("1."):
        raise RuntimeError(f"unsupported pyperf JSON version: {version}")
    benchmarks = payload.get("benchmarks")
    if not isinstance(benchmarks, list) or not benchmarks:
        raise RuntimeError("official pyperformance JSON has no benchmark entries")
    return payload


def merged_metadata(entry: dict[str, Any], suite_metadata: dict[str, Any]) -> dict[str, Any]:
    metadata = entry.get("metadata")
    if metadata is None:
        return dict(suite_metadata)
    if not isinstance(metadata, dict):
        raise TypeError("benchmark metadata is not an object")
    return {**suite_metadata, **metadata}


def benchmark_name(metadata: dict[str, Any], index: int) -> str:
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"benchmark entry {index} is missing its official name")
    return name


def collect_values(entry: dict[str, Any], name: str) -> list[float]:
    runs = entry.get("runs")
    if not isinstance(runs, list) or not runs:
        raise RuntimeError(f"benchmark {name} has no runs")
    values: list[float] = []
    for run in runs:
        if not isinstance(run, dict):
            raise TypeError(f"benchmark {name} has a malformed run entry")
        run_values = run.get("values")
        # pyperformance writes an initial calibration record containing only
        # warmups. It is not a measured run and must not contribute to the
        # reported median.
        if run_values is None and "values" not in run:
            warmups = run.get("warmups")
            if isinstance(warmups, list) and warmups:
                continue
        if not isinstance(run_values, list) or not run_values:
            raise RuntimeError(f"benchmark {name} has a run without values")
        for value in run_values:
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise RuntimeError(f"benchmark {name} has an invalid run value")
            values.append(float(value))
    if not values:
        raise RuntimeError(f"benchmark {name} has no measured values")
    return values


def normalize_results(
    payload: dict[str, Any]
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    suite_metadata = payload.get("metadata")
    if suite_metadata is None:
        suite_metadata = {}
    if not isinstance(suite_metadata, dict):
        raise TypeError("suite metadata is not an object")

    results: dict[str, dict[str, Any]] = {}
    runtime_context: dict[str, Any] = {}
    for index, entry in enumerate(payload["benchmarks"]):
        if not isinstance(entry, dict):
            raise TypeError("benchmark entry is not an object")
        metadata = merged_metadata(entry, suite_metadata)
        name = benchmark_name(metadata, index)
        if name in results:
            raise RuntimeError(f"official output has duplicate benchmark: {name}")
        unit = metadata.get("unit", "second")
        if unit not in UNIT_TO_SECONDS:
            raise RuntimeError(f"benchmark {name} has unsupported unit: {unit}")
        values = collect_values(entry, name)
        median = statistics.median(values)
        seconds = median * UNIT_TO_SECONDS[unit]
        if not math.isfinite(seconds) or seconds <= 0:
            raise RuntimeError(f"benchmark {name} has an invalid median value")
        results[name] = {
            "source_name": name,
            "source_field": "median",
            "raw_value": median,
            "raw_unit": unit,
            "value": seconds,
            "unit": "s",
        }
        if not runtime_context:
            for key in RUNTIME_CONTEXT_KEYS:
                if key in metadata:
                    runtime_context[key] = metadata[key]
    if not results:
        raise RuntimeError("official pyperformance JSON produced no benchmarks")
    return results, runtime_context


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
        results, runtime_context = normalize_results(payload)
    except (RuntimeError, TypeError, ValueError) as exc:
        print(f"[python-parse] ERROR: {exc}", file=sys.stderr)
        return 1

    benchmark_names = list(results)
    version = os.environ["SOFTWARE_VERSION"]
    architecture = os.environ["EXPECTED_ARCH"]
    pyperformance_version = os.environ.get("PYPERFORMANCE_VERSION", "")
    warmup = os.environ.get("PYPERFORMANCE_WARMUP", "")
    configure_options = os.environ.get("CONFIGURE_OPTIONS", "").split()
    normalized = {
        "benchmark": "python_official_pyperformance",
        "software": "python",
        "version": version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "command": [
                "<built-python>",
                "-m",
                "pyperformance",
                "run",
                "--warmup",
                warmup,
                "--inherit-environ",
                "PIP_INDEX_URL,PIP_TRUSTED_HOST,http_proxy,https_proxy,no_proxy,all_proxy,HTTP_PROXY,HTTPS_PROXY,NO_PROXY,ALL_PROXY",
                "-o",
                "benchmark.json",
            ],
            "pyperformance_version": pyperformance_version,
            "benchmark_group": "default",
            "benchmarks": benchmark_names,
            "build_options": configure_options,
            "aggregation": "median",
            "official_suite": "pyperformance",
        },
        "metric_contract": {
            "scope": "median of every completed default pyperformance benchmark",
            "source_field": "median",
            "normalized_unit": "s",
            "direction": "lower_is_better",
            "aggregation": "median",
        },
        "runtime_context": runtime_context,
        "results": results,
    }

    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[python-parse] normalized {len(results)} official benchmarks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
