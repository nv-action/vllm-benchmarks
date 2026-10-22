#!/usr/bin/env python3
"""Normalize the sonic_bench.sh result JSON into the framework metric schema.

Input is the JSON produced by scripts/go/sonic_bench.sh (documented shape:
runs / results arrays with per-mode exit codes). Output keeps the original
subject / mode / benchmark / unit names without renaming, converts every
measurement into a metric, and fails on any non-zero per-run exit code so a
failed benchmark can never exit successfully.
"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UNIT_DIRECTIONS = {
    "ns/op": "lower_is_better",
    "B/op": "lower_is_better",
    "allocs/op": "lower_is_better",
}
ZERO_ALLOWED_UNITS = {"B/op", "allocs/op"}
FIELDS = (
    ("ns_per_op", "ns/op"),
    ("bytes_per_op", "B/op"),
    ("allocs_per_op", "allocs/op"),
)


def metric_name(subject: str, mode: str, benchmark: str, unit: str) -> str:
    return f"{subject} :: {mode} :: {benchmark} :: {unit}"


def load_raw(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"cannot read sonic_bench result JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise TypeError("sonic_bench result JSON root must be an object")
    return payload


def validate_runs(payload: dict[str, Any]) -> list[dict[str, Any]]:
    runs = payload.get("runs")
    if not isinstance(runs, list) or not runs:
        raise RuntimeError("sonic_bench result JSON has no runs")
    failed = [
        f"{run.get('subject')}/{run.get('mode')} (exit={run.get('exit_code')})"
        for run in runs
        if isinstance(run, dict) and int(run.get("exit_code", 1)) != 0
    ]
    if failed:
        raise RuntimeError(f"sonic benchmark runs failed: {', '.join(failed)}")
    return runs


def normalize(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("results")
    if not isinstance(rows, list) or not rows:
        raise RuntimeError("sonic_bench result JSON has no results")
    metrics: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise TypeError("sonic benchmark result row must be an object")
        subject = row.get("subject")
        mode = row.get("mode")
        benchmark = row.get("benchmark")
        if not subject or not mode or not benchmark:
            raise RuntimeError(f"sonic benchmark result row is incomplete: {row}")
        for key, unit in FIELDS:
            raw_value = row.get(key)
            if raw_value is None or raw_value == "":
                continue
            try:
                value = float(raw_value)
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"sonic benchmark {benchmark} has invalid {unit}: {raw_value}"
                ) from exc
            if not math.isfinite(value) or value < 0:
                raise RuntimeError(
                    f"sonic benchmark {benchmark} has invalid {unit}: {value}"
                )
            if value == 0 and unit not in ZERO_ALLOWED_UNITS:
                raise RuntimeError(
                    f"sonic benchmark {benchmark} has invalid {unit}: {value}"
                )
            source_name = metric_name(subject, mode, benchmark, unit)
            if source_name in metrics:
                raise RuntimeError(
                    f"duplicate sonic benchmark measurement: {source_name}"
                )
            metrics[source_name] = {
                "source_name": source_name,
                "source_subject": subject,
                "source_mode": mode,
                "source_benchmark": benchmark,
                "source_field": unit,
                "group": f"{subject}/{mode}",
                "value": value,
                "unit": unit,
                "direction": UNIT_DIRECTIONS[unit],
            }
    if not metrics:
        raise RuntimeError("sonic_bench result JSON contains no measurements")
    return {key: metrics[key] for key in sorted(metrics)}


def main() -> int:
    if len(sys.argv) != 3:
        print(
            "usage: parse_sonic_bench.py RAW_RESULT_JSON NORMALIZED_OUTPUT",
            file=sys.stderr,
        )
        return 1
    raw_path = Path(sys.argv[1])
    output_path = Path(sys.argv[2])
    try:
        payload = load_raw(raw_path)
        validate_runs(payload)
        metrics = normalize(payload)
        environment = payload.get("env") if isinstance(payload.get("env"), dict) else {}
        rendered_env = {
            key: environment.get(key, "")
            for key in (
                "cpu_bind",
                "goexperiment",
                "goarm64",
                "gcflags",
                "ldflags",
                "sonic_no_async_gc",
                "sonic_dir",
                "encoder_pkg",
                "decoder_pkg",
                "parser_pkg",
            )
        }
        rendered_env["benchtime"] = payload.get("benchtime", "")
        rendered_env["toolchain_version"] = payload.get("toolchain_version", "")
        normalized = {
            "software": "sonic-go",
            "version": os.environ.get("SOFTWARE_VERSION", ""),
            "architecture": os.environ.get("EXPECTED_ARCH", ""),
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "benchmark": "bytedance_sonic_go_test_bench",
            "command": str(payload.get("test_cmd", "")),
            "environment": rendered_env,
            "results": metrics,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"[sonic-go-parse] ERROR: {exc}", file=sys.stderr)
        return 1
    print(f"[sonic-go-parse] normalized {len(metrics)} sonic benchmark measurements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
