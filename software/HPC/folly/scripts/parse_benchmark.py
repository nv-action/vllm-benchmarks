#!/usr/bin/env python3
"""Normalize the output of Folly's official CMake BENCHMARK targets.

Most targets use folly/Benchmark.cpp and write the official
``--bm_json_verbose`` array. Two official targets print their own tables
instead; their raw stdout is parsed using the exact printed column names.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def load_manifest(path: Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read benchmark manifest: {exc}") from exc
    if not isinstance(payload, list) or not payload:
        raise RuntimeError("benchmark manifest must be a non-empty list")
    for entry in payload:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("dir"), str)
            or not isinstance(entry.get("target"), str)
        ):
            raise TypeError("benchmark manifest entries need string dir/target")
    return payload


def load_json_target_results(path: Path, target: str) -> list[Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(
            f"cannot read --bm_json_verbose output of {target}: {exc}"
        ) from exc
    if not isinstance(payload, list) or not payload:
        raise RuntimeError(f"benchmark target {target} produced no benchmark rows")
    return payload


def to_metric(entry: Any, target: str, directory: str) -> dict[str, Any]:
    # Official --bm_json_verbose row: [file, name, timeInNs, counters?]
    if not isinstance(entry, list) or len(entry) < 3:
        raise TypeError(
            f"benchmark target {target} produced a malformed row: {entry!r}"
        )
    name = entry[1]
    if not isinstance(name, str) or not name:
        raise RuntimeError(f"benchmark target {target} has a row without a name")
    raw_value = entry[2]
    if isinstance(raw_value, bool) or not isinstance(raw_value, (int, float)):
        raise TypeError(f"benchmark {target}/{name} has a non-numeric timeInNs value")
    value = float(raw_value)
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"benchmark {target}/{name} has an invalid timeInNs value")
    source_name = f"{directory}/{target}/{name}"
    return {
        "source_name": source_name,
        "source_field": "timeInNs",
        "raw_value": value,
        "raw_unit": "ns",
        "value": value,
        "unit": "ns",
        "direction": "lower_is_better",
    }


def custom_metric(
    target: str,
    directory: str,
    threads: str,
    scenario: str,
    column: str,
    value: str,
) -> dict[str, Any]:
    numeric_value = float(value)
    if not math.isfinite(numeric_value) or numeric_value < 0:
        raise RuntimeError(
            f"benchmark {target}/{scenario}/{column} has an invalid value"
        )
    source_name = (
        f"{directory}/{target}/threads={threads}/{scenario}/{column} (ns/op)"
    )
    return {
        "source_name": source_name,
        "source_field": column,
        "raw_value": numeric_value,
        "raw_unit": "ns/op",
        "value": numeric_value,
        "unit": "ns/op",
        "direction": "lower_is_better",
    }


def parse_concurrent_hash_map_output(
    raw_output: str, target: str, directory: str
) -> list[dict[str, Any]]:
    thread_pattern = re.compile(r"^=+\s*(\d+)\s+threads")
    row_pattern = re.compile(
        r"^(CHM .+?)\s+(\d+)\s+ns\s+(\d+)\s+ns\s+(\d+)\s+ns$"
    )
    threads: str | None = None
    metrics: list[dict[str, Any]] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        thread_match = thread_pattern.match(line)
        if thread_match:
            threads = thread_match.group(1)
            continue
        row_match = row_pattern.match(line)
        if row_match:
            if threads is None:
                raise RuntimeError(f"benchmark {target} emitted a row before a thread count")
            scenario, maximum, average, minimum = row_match.groups()
            for column, value in (
                ("Max time", maximum),
                ("Avg time", average),
                ("Min time", minimum),
            ):
                metrics.append(
                    custom_metric(target, directory, threads, scenario, column, value)
                )
    if not metrics:
        raise RuntimeError(f"benchmark target {target} produced no parseable rows")
    return metrics


def parse_request_context_output(
    raw_output: str, target: str, directory: str
) -> list[dict[str, Any]]:
    thread_pattern = re.compile(r"^=+\s*(\d+)\s+threads")
    row_pattern = re.compile(
        r"^(.+?)\s+(\d+)\s+ns\s+(\d+)\s+ns\s+(\d+)\s+ns\s+(\d+)\s+ns$"
    )
    threads: str | None = None
    metrics: list[dict[str, Any]] = []
    for raw_line in raw_output.splitlines():
        line = raw_line.strip()
        thread_match = thread_pattern.match(line)
        if thread_match:
            threads = thread_match.group(1)
            continue
        row_match = row_pattern.match(line)
        if row_match:
            if threads is None:
                raise RuntimeError(f"benchmark {target} emitted a row before a thread count")
            scenario, maximum, average, deviation, minimum = row_match.groups()
            for column, value in (
                ("Max time", maximum),
                ("Avg time", average),
                ("Dev time", deviation),
                ("Min time", minimum),
            ):
                metrics.append(
                    custom_metric(target, directory, threads, scenario, column, value)
                )
    if not metrics:
        raise RuntimeError(f"benchmark target {target} produced no parseable rows")
    return metrics


CUSTOM_OUTPUT_PARSERS = {
    "concurrency_concurrent_hash_map_bench": parse_concurrent_hash_map_output,
    "io_async_request_context_benchmark": parse_request_context_output,
}


def load_custom_target_results(
    path: Path, target: str, directory: str
) -> list[dict[str, Any]]:
    parser = CUSTOM_OUTPUT_PARSERS.get(target)
    if parser is None:
        raise RuntimeError(
            f"benchmark target {target} produced no --bm_json_verbose output and "
            "has no declared raw-output parser"
        )
    try:
        raw_output = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"cannot read raw output of {target}: {exc}") from exc
    return parser(raw_output, target, directory)


def normalize_results(
    manifest: list[dict[str, Any]], bench_json_dir: Path, bench_stdout_dir: Path
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for entry in manifest:
        target, directory = entry["target"], entry["dir"]
        target_path = bench_json_dir / f"{target}.json"
        if target_path.is_file() and target_path.stat().st_size > 0:
            metrics = [
                to_metric(row, target, directory)
                for row in load_json_target_results(target_path, target)
            ]
        else:
            metrics = load_custom_target_results(
                bench_stdout_dir / f"{target}.log", target, directory
            )
        for metric in metrics:
            source_name = metric["source_name"]
            if source_name in results:
                raise RuntimeError(
                    f"official benchmarks produced duplicate scenario: {source_name}"
                )
            results[source_name] = metric
    if not results:
        raise RuntimeError("official benchmarks produced no scenarios")
    return results


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: parse_benchmark.py MANIFEST_JSON BENCH_JSON_DIR "
            "BENCH_STDOUT_DIR NORMALIZED_OUTPUT",
            file=sys.stderr,
        )
        return 1
    manifest_path, bench_json_dir, bench_stdout_dir, normalized_output = map(
        Path, sys.argv[1:]
    )

    try:
        manifest = load_manifest(manifest_path)
        results = normalize_results(manifest, bench_json_dir, bench_stdout_dir)
    except (RuntimeError, TypeError, ValueError) as exc:
        print(f"[folly-parse] ERROR: {exc}", file=sys.stderr)
        return 1

    version = os.environ["SOFTWARE_VERSION"]
    architecture = os.environ["EXPECTED_ARCH"]
    normalized = {
        "benchmark": "folly_official_benchmarks",
        "software": "folly",
        "version": version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "official_entry": (
                "official BENCHMARK targets of CMakeLists.txt; standard targets "
                "use --bm_json_verbose and the two official custom-table targets "
                "retain and parse their stdout"
            ),
            "command": ["<target>", "--bm_json_verbose=<output>", "> <target>.log"],
            "benchmark_targets": len(manifest),
            "metric_source": (
                "timeInNs of --bm_json_verbose rows, or the verbatim time "
                "columns of official custom benchmark tables"
            ),
        },
        "metric_contract": {
            "scope": "every parsed time value of every official target",
            "source_field": "timeInNs or a verbatim official table column",
            "normalized_unit": "ns or ns/op",
            "direction": "lower_is_better",
        },
        "results": results,
    }

    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[folly-parse] normalized {len(results)} official scenarios")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
