#!/usr/bin/env python3
"""Parse the official Kubernetes go test -bench output into framework results.

The benchmark driver runs the upstream test binaries of selected Kubernetes
packages and tees their console output into benchmark_kubernetes_raw.log.
Section markers inserted by the driver delimit which package (and metric
group) each benchmark belongs to.  This script parses that log, writes the
structured benchmark_kubernetes.json, and emits the aggregate results.json
consumed by the framework metrics collection.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
from pathlib import Path
from typing import Any


MARKER_RE = re.compile(r"^### PACKAGE (\S+) (\S+)$")
BENCH_RE = re.compile(r"^(Benchmark\S+?)(?:-\d+)?\s+(\d+)\s+(.*)$")
FAILURE_RE = re.compile(r"^(FAIL|panic:|--- FAIL)")

# Recognised benchmark columns: unit label, metric suffix and direction.
COLUMN_MAP = {
    "ns/op": ("time", "us", 1000.0),
    "B/op": ("bytes", "B", 1.0),
    "allocs/op": ("allocs", "allocs/op", 1.0),
}


def parse_bench_line(rest: str) -> list[tuple[str, float, str]] | None:
    tokens = rest.split()
    if len(tokens) < 2 or len(tokens) % 2 != 0:
        return None
    metrics: list[tuple[str, float, str]] = []
    index = 0
    while index + 1 < len(tokens):
        raw_value, label = tokens[index], tokens[index + 1]
        if label not in COLUMN_MAP:
            return None
        try:
            value = float(raw_value)
        except ValueError:
            return None
        suffix, unit, divisor = COLUMN_MAP[label]
        metrics.append((suffix, value / divisor, unit))
        index += 2
    return metrics or None


def parse_log(raw_log: Path) -> dict[str, Any]:
    sections: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    failures: list[str] = []

    for line in raw_log.read_text(encoding="utf-8", errors="replace").splitlines():
        stripped = line.strip()
        marker = MARKER_RE.match(stripped)
        if marker:
            package, group = marker.group(1), marker.group(2)
            if group in sections:
                raise RuntimeError(f"duplicate benchmark group {group}")
            current = {"package": package, "group": group, "benchmarks": {}}
            sections[group] = current
            continue
        if FAILURE_RE.match(stripped):
            failures.append(stripped)
            continue
        match = BENCH_RE.match(stripped)
        if not match or current is None:
            continue
        bench_name = match.group(1)
        iterations = int(match.group(2))
        if iterations < 1 or bench_name in current["benchmarks"]:
            continue
        metrics = parse_bench_line(match.group(3))
        if not metrics:
            continue
        current["benchmarks"][bench_name] = {
            "iterations": iterations,
            "metrics": {
                suffix: {"value": value, "unit": unit}
                for suffix, value, unit in metrics
            },
        }

    if failures:
        raise RuntimeError(f"benchmark driver reported failures: {failures[0]}")
    if not sections:
        raise RuntimeError("no benchmark package sections were found")
    for section in sections.values():
        if not section["benchmarks"]:
            raise RuntimeError(
                f"package {section['package']} produced no benchmark results")
        for bench_name, bench in section["benchmarks"].items():
            if "time" not in bench["metrics"]:
                raise RuntimeError(
                    f"benchmark {bench_name} has no ns/op measurement")
            for metric in bench["metrics"].values():
                value = metric["value"]
                if not math.isfinite(value) or value <= 0:
                    raise RuntimeError(
                        f"benchmark {bench_name} produced a non-positive metric")
    return sections


def build_report(sections: dict[str, dict[str, Any]], identity: dict[str, Any],
                 parameters: dict[str, Any]) -> dict[str, Any]:
    ordered: dict[str, dict[str, Any]] = {}
    for group in sorted(sections):
        section = sections[group]
        for bench_name in sorted(section["benchmarks"]):
            bench = section["benchmarks"][bench_name]
            for suffix in ("time", "bytes", "allocs"):
                metric = bench["metrics"].get(suffix)
                if not metric:
                    continue
                ordered[f"{bench_name} {suffix}"] = {
                    "source_name": f"{bench_name} {suffix}",
                    "value": round(metric["value"], 6),
                    "unit": metric["unit"],
                    "direction": "lower_is_better",
                    "group": group,
                }
    if not ordered:
        raise RuntimeError("no usable metrics were collected")
    return {
        "benchmark": "k8s_go_test_bench",
        "description": "Official Kubernetes go test -bench benchmarks",
        "reference": identity["reference"],
        "software": "kubernetes",
        "version": identity["version"],
        "go_version": identity["go_version"],
        "architecture": identity["architecture"],
        "packages": [
            {"package": section["package"], "group": group}
            for group, section in sorted(sections.items())
        ],
        "parameters": parameters,
        "results": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--raw-log", required=True, type=Path)
    parser.add_argument("--results-dir", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--benchtime", required=True)
    args = parser.parse_args()

    software_version = os.environ.get("SOFTWARE_VERSION", "unknown")
    expected_arch = os.environ.get("EXPECTED_ARCH", "unknown")
    run_id = os.environ.get("PERF_RUN_ID", "")
    go_version = os.environ.get("GO_VERSION", "unknown")
    reference = os.environ.get(
        "KUBERNETES_REPOSITORY", "https://github.com/kubernetes/kubernetes")

    sections = parse_log(args.raw_log)
    identity = {
        "reference": reference,
        "version": software_version,
        "go_version": go_version,
        "architecture": expected_arch,
    }
    report = build_report(
        sections, identity, {"benchtime": args.benchtime, "count": 1})

    args.results_dir.joinpath("benchmark_kubernetes.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    args.output.write_text(
        json.dumps(
            {
                "software": "kubernetes",
                "version": software_version,
                "architecture": expected_arch,
                "run_id": run_id,
                "results": report["results"],
            },
            indent=2,
        ) + "\n",
        encoding="utf-8",
    )
    print(f"[aggregate] {len(report['results'])} metrics saved to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
