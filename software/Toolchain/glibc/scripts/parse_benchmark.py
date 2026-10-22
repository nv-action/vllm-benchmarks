#!/usr/bin/env python3
"""Extract comparable public memcpy/memcmp timings from glibc 2.31 benchtests."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TIMING_TYPE = "clock_gettime_ns"
LENGTHS = (2048, 4096)


def read_benchmark(directory: Path, function: str) -> tuple[dict[str, Any], str]:
    source_file = f"bench-{function}.out"
    path = directory / source_file
    if not path.is_file() or not path.stat().st_size:
        raise ValueError(f"missing or empty official output: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("timing_type") != TIMING_TYPE:
        raise ValueError(f"{source_file} does not use {TIMING_TYPE}")
    functions = payload.get("functions")
    if not isinstance(functions, dict) or list(functions) != [function]:
        raise ValueError(f"{source_file} has an unexpected function")
    benchmark = functions[function]
    implementations = benchmark.get("ifuncs")
    rows = benchmark.get("results")
    if not isinstance(implementations, list) or implementations.count(function) != 1:
        raise ValueError(f"{source_file} must include the public {function} entry")
    if not isinstance(rows, list) or not rows:
        raise ValueError(f"{source_file} has no benchmark results")
    return benchmark, source_file


def extract_metrics(directory: Path) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for function in ("memcpy", "memcmp"):
        benchmark, source_file = read_benchmark(directory, function)
        implementation_index = benchmark["ifuncs"].index(function)
        for length in LENGTHS:
            scenario = {"length": length, "align1": 0, "align2": 0}
            if function == "memcmp":
                scenario["expected_result"] = 0
            matches = [
                (index, row)
                for index, row in enumerate(benchmark["results"])
                if isinstance(row, dict)
                and all(row.get(key) == value for key, value in scenario.items())
            ]
            if not matches:
                raise ValueError(
                    f"{source_file} has no {scenario} scenario"
                )
            # memcpy repeats the page-size case after its power-of-two sweep.
            # The first matching row always belongs to the common sweep on
            # both architectures, regardless of the host page size.
            row_index, row = matches[0]
            timings = row.get("timings")
            if not isinstance(timings, list) or len(timings) != len(benchmark["ifuncs"]):
                raise ValueError(f"{source_file} has malformed timing values")
            value = timings[implementation_index]
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError(f"{source_file} contains an invalid public timing: {value!r}")
            name = f"{function}.length={length}.align1=0.align2=0"
            if function == "memcmp":
                name += ".expected_result=0"
            results[name] = {
                "source_name": name,
                "source_field": (
                    f"functions.{function}.results[{row_index}]."
                    f"timings[{implementation_index}]"
                ),
                "source_file": source_file,
                "implementation": function,
                "group": function,
                "raw_value": value,
                "raw_unit": "ns",
                "value": value,
                "unit": "ns",
            }
    return results


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: parse_benchmark.py BENCHTESTS_DIR OUTPUT_JSON", file=sys.stderr)
        return 1
    directory, output_path = Path(sys.argv[1]), Path(sys.argv[2])
    if os.environ.get("TIMING_TYPE_OUTPUT") != TIMING_TYPE:
        print("[glibc-parse] ERROR: bench-timing-type is not clock_gettime_ns", file=sys.stderr)
        return 1
    try:
        results = extract_metrics(directory)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"[glibc-parse] ERROR: {exc}", file=sys.stderr)
        return 1
    payload = {
        "benchmark": "glibc_2.31_string_benchtests",
        "software": "glibc",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "build_targets": ["bench-timing-type", "bench-memcpy", "bench-memcmp"],
            "run_commands": ["bench-memcpy", "bench-memcmp"],
            "timing_backend": "clock_gettime(CLOCK_MONOTONIC)",
            "benchtest_patch": "scripts/glibc-2.31-benchtests.patch",
            "selected_lengths": list(LENGTHS),
        },
        "results": results,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[glibc-parse] normalized {len(results)} public glibc timings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
