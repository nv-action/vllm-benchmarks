#!/usr/bin/env python3
"""Normalize output from the official Bent benchmark command.

The Go benchmark format records one benchmark name followed by one or more
``value unit`` pairs. A metric is identified by the exact benchmark name, its
official package tag, and its original unit. This adapter creates no workload
name, metric name, or derived score.
"""

from __future__ import annotations

import json
import math
import os
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

BENCHMARK_LINE = re.compile(
    r"^(?P<name>Benchmark\S+)\s+(?P<iterations>\d+)\s+(?P<values>.+)$"
)
MEASUREMENT = re.compile(
    r"(?P<value>[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s+(?P<unit>\S+)"
)
TAG_LINE = re.compile(r"^(?P<key>pkg|shortname|toolchain|pgo):\s*(?P<value>.+)$")


def fail(message: str) -> None:
    print(f"[golang-parse] ERROR: {message}", file=sys.stderr)


def direction_for_unit(unit: str) -> str:
    """Return the direction inherent in the original Go benchmark unit."""
    if unit.endswith(("/s", "/sec")):
        return "higher_is_better"
    return "lower_is_better"


def metric_name(package: str, benchmark: str, unit: str) -> str:
    """Return the complete official benchmark measurement identity."""
    return f"{package} :: {benchmark} :: {unit}"


def parse_output(lines: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    samples: dict[tuple[str, str, str], list[float]] = {}
    tags: dict[str, str] = {}
    suites: set[str] = set()
    bent_measurements = 0

    for line in lines:
        tag_match = TAG_LINE.match(line)
        if tag_match is not None:
            tags[tag_match.group("key")] = tag_match.group("value").strip()
            continue

        line_match = BENCHMARK_LINE.match(line)
        if line_match is None:
            continue

        benchmark = line_match.group("name")
        package = tags.get("pkg", "")
        if not package:
            raise RuntimeError(f"benchmark {benchmark} has no preceding official pkg tag")
        measurements = list(MEASUREMENT.finditer(line_match.group("values")))
        if not measurements:
            raise RuntimeError(f"benchmark {benchmark} has no value/unit measurements")
        suites.add(tags.get("shortname", package))

        for measurement in measurements:
            value = float(measurement.group("value"))
            unit = measurement.group("unit")
            if not math.isfinite(value) or value < 0:
                raise RuntimeError(f"benchmark {benchmark} has invalid {unit} value: {value}")
            key = (package, benchmark, unit)
            samples.setdefault(key, []).append(value)
            bent_measurements += 1

    if not samples:
        raise RuntimeError("selected official Go benchmark output contains no measurements")
    if bent_measurements == 0:
        raise RuntimeError("official Bent benchmark produced no measurements")

    results: dict[str, dict[str, Any]] = {}
    for (package, benchmark, unit), values in sorted(samples.items()):
        name = metric_name(package, benchmark, unit)
        if name in results:
            raise RuntimeError(f"duplicate official benchmark measurement: {name}")
        value = statistics.median(values)
        results[name] = {
            "source_name": name,
            "source_benchmark": benchmark,
            "source_package": package,
            "source_field": unit,
            "raw_value": value,
            "raw_unit": unit,
            "value": value,
            "unit": unit,
            "direction": direction_for_unit(unit),
            "samples": len(values),
            "group": package,
        }

    return results, {
        "packages": sorted({key[0] for key in samples}),
        "suites": sorted(suites),
        "bent_measurements": bent_measurements,
    }


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: parse_benchmark.py RAW_OUTPUT NORMALIZED_OUTPUT", file=sys.stderr)
        return 1
    raw_path, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    try:
        version = os.environ["SOFTWARE_VERSION"]
        architecture = os.environ["EXPECTED_ARCH"]
    except KeyError as exc:
        fail(f"missing environment variable: {exc}")
        return 1

    try:
        results, context = parse_output(
            raw_path.read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except (OSError, RuntimeError, ValueError) as exc:
        fail(str(exc))
        return 1

    payload = {
        "benchmark": "golang_official_bent",
        "software": "golang",
        "version": version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "components": {
                "install": ["go", "install", "golang.org/x/benchmarks/cmd/bent@latest"],
                "run": ["bent", "-N", "15"],
            },
            "suite": "golang.org/x/benchmarks/cmd/bent",
            "configuration_files": [
                "suites.toml",
                "benchmarks-100.toml",
                "configurations.toml",
            ],
            "configuration": {
                "name": "Baseline",
                "run_environment": ["GOGC=100", "GOMAXPROCS=$(nproc)"],
            },
            "aggregation": "median",
        },
        "metric_contract": {
            "scope": "every value/unit pair emitted by the official Bent benchmark command",
            "identity": "official pkg tag + benchmark name + original unit",
            "aggregation": "median of repeated official measurements",
        },
        "runtime_context": context,
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[golang-parse] normalized {len(results)} official benchmark measurements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
