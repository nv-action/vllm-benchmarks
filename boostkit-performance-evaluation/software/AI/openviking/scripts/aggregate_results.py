#!/usr/bin/env python3
"""Aggregate the OpenViking benchmark outputs into framework results.json."""

from __future__ import annotations

import argparse
import json
import math
import os
from pathlib import Path
from typing import Any


CONTEXT_OPERATIONS = ("write", "read", "stat", "ls", "rm", "mkdir", "grep")
CONTEXT_SIZES = ("small_1kb", "medium_64kb", "large_1mb")
MICRO_OPERATIONS = ("write", "read", "stat", "ls", "rm")
MICRO_SIZES = ("1kb", "64kb", "1mb")
MICRO_THREAD_COUNTS = (1, 2, 4, 8, 32)
CLIENT_INIT_FIELDS = ("client_init_ms", "health_check_ms", "get_capabilities_ms")


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise RuntimeError(f"{path} is not a JSON object")
    return payload


def record(results: dict[str, dict[str, Any]], name: str, group: str,
           value: Any, unit: str, direction: str) -> bool:
    """Add one metric; return False when the value is unusable."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        return False
    results[name] = {
        "source_name": name,
        "value": round(value, 6),
        "unit": unit,
        "direction": direction,
        "group": group,
    }
    return True


def collect_context_metrics(context: dict[str, Any],
                            results: dict[str, dict[str, Any]]) -> None:
    summary = context.get("results_summary", {})
    if not isinstance(summary, dict) or not summary:
        raise RuntimeError("benchmark_context.json has no results_summary")
    for size_name in CONTEXT_SIZES:
        size_results = summary.get(size_name)
        if not isinstance(size_results, dict):
            raise RuntimeError(f"context benchmark is missing size {size_name}")
        for op in CONTEXT_OPERATIONS:
            if not record(results,
                          f"context_fs {size_name} {op}_ops_per_sec",
                          "context_fs",
                          size_results.get(f"{op}_ops_per_sec"),
                          "ops/s", "higher_is_better"):
                raise RuntimeError(
                    f"context_fs {size_name} {op}_ops_per_sec is unusable")
            if not record(results,
                          f"context_fs {size_name} {op}_avg_latency_ms",
                          "context_fs",
                          size_results.get(f"{op}_avg_latency_ms"),
                          "ms", "lower_is_better"):
                raise RuntimeError(
                    f"context_fs {size_name} {op}_avg_latency_ms is unusable")
        for op in ("write", "read"):
            if not record(results,
                          f"context_fs {size_name} {op}_throughput_mbs",
                          "context_fs",
                          size_results.get(f"{op}_throughput_mbs"),
                          "MB/s", "higher_is_better"):
                raise RuntimeError(
                    f"context_fs {size_name} {op}_throughput_mbs is unusable")


def collect_micro_metrics(micro: dict[str, Any],
                          results: dict[str, dict[str, Any]]) -> None:
    measurements = micro.get("results", {})
    if not isinstance(measurements, dict) or not measurements:
        raise RuntimeError("micro_benchmark.json has no results")

    import_init = measurements.get("import_init")
    if not isinstance(import_init, dict):
        raise RuntimeError("micro benchmark is missing import_init")
    for field in ("import_time_ms", "reload_time_ms"):
        if not record(results, f"micro import_init {field}", "import",
                      import_init.get(field), "ms", "lower_is_better"):
            raise RuntimeError(f"micro import_init {field} is unusable")

    client_init = measurements.get("client_init")
    if not isinstance(client_init, dict):
        raise RuntimeError("micro benchmark is missing client_init")
    for field in CLIENT_INIT_FIELDS:
        if not record(results, f"micro client_init {field}", "client_init",
                      client_init.get(field), "ms", "lower_is_better"):
            raise RuntimeError(f"micro client_init {field} is unusable")

    fs_ops = measurements.get("fs_ops_latency")
    if not isinstance(fs_ops, dict) or not fs_ops:
        raise RuntimeError("micro benchmark is missing fs_ops_latency")
    for size_name in MICRO_SIZES:
        size_results = fs_ops.get(size_name)
        if not isinstance(size_results, dict):
            raise RuntimeError(f"micro fs_ops is missing size {size_name}")
        for op in MICRO_OPERATIONS:
            if not record(results,
                          f"micro fs_ops {size_name} {op}_avg_latency_ms",
                          "fs_ops",
                          size_results.get(f"{op}_avg_latency_ms"),
                          "ms", "lower_is_better"):
                raise RuntimeError(
                    f"micro fs_ops {size_name} {op}_avg_latency_ms is unusable")
            if not record(results,
                          f"micro fs_ops {size_name} {op}_ops_per_sec",
                          "fs_ops",
                          size_results.get(f"{op}_ops_per_sec"),
                          "ops/s", "higher_is_better"):
                raise RuntimeError(
                    f"micro fs_ops {size_name} {op}_ops_per_sec is unusable")
        for op in ("write", "read"):
            if not record(results,
                          f"micro fs_ops {size_name} {op}_throughput_mbs",
                          "fs_ops",
                          size_results.get(f"{op}_throughput_mbs"),
                          "MB/s", "higher_is_better"):
                raise RuntimeError(
                    f"micro fs_ops {size_name} {op}_throughput_mbs is unusable")

    multithread = measurements.get("multithread_fs_ops")
    if not isinstance(multithread, dict) or not multithread:
        raise RuntimeError("micro benchmark is missing multithread_fs_ops")
    for thread_count in MICRO_THREAD_COUNTS:
        thread_results = multithread.get(f"threads_{thread_count}")
        if not isinstance(thread_results, dict):
            raise RuntimeError(
                f"micro multithread is missing threads_{thread_count}")
        if not record(results,
                      f"micro multithread threads_{thread_count} "
                      "combined_ops_per_sec",
                      "multithread",
                      thread_results.get("combined_ops_per_sec"),
                      "ops/s", "higher_is_better"):
            raise RuntimeError(
                f"micro multithread threads_{thread_count} "
                "combined_ops_per_sec is unusable")
        if not record(results,
                      f"micro multithread threads_{thread_count} "
                      "avg_per_op_latency_ms",
                      "multithread",
                      thread_results.get("avg_per_op_latency_ms"),
                      "ms", "lower_is_better"):
            raise RuntimeError(
                f"micro multithread threads_{thread_count} "
                "avg_per_op_latency_ms is unusable")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    context = load_json(args.results_dir / "benchmark_context.json")
    micro = load_json(args.results_dir / "micro_benchmark.json")
    for source in (context, micro):
        if source.get("software") != "openviking":
            raise RuntimeError("benchmark output has an invalid software identity")

    software_version = os.environ.get("SOFTWARE_VERSION", context.get("version"))
    architecture = os.environ.get("EXPECTED_ARCH", context.get("architecture"))
    run_id = os.environ.get("PERF_RUN_ID", "")
    if context.get("version") != micro.get("version"):
        raise RuntimeError(
            f"benchmark version mismatch: context={context.get('version')} "
            f"micro={micro.get('version')}")
    if software_version and context.get("version") != software_version:
        raise RuntimeError(
            f"benchmark version {context.get('version')} differs from the "
            f"requested {software_version}")

    results: dict[str, dict[str, Any]] = {}
    collect_context_metrics(context, results)
    collect_micro_metrics(micro, results)
    if not results:
        raise RuntimeError("no usable metrics were collected")

    aggregate = {
        "software": "openviking",
        "version": software_version,
        "architecture": architecture,
        "run_id": run_id,
        "results": results,
    }
    args.output.write_text(
        json.dumps(aggregate, indent=2) + "\n", encoding="utf-8")
    print(f"[aggregate] {len(results)} metrics saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
