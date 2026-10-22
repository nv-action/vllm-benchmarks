#!/usr/bin/env python3
"""聚合 OceanBase OLTP 与微基准结果，生成 Framework 可消费的 results.json。"""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone

FIELD_DEFS = {
    "tps": {"unit": "trans/sec", "direction": "higher_is_better"},
    "qps": {"unit": "queries/sec", "direction": "higher_is_better"},
    "avg_latency_ms": {"unit": "ms", "direction": "lower_is_better"},
    "p95_latency_ms": {"unit": "ms", "direction": "lower_is_better"},
}
MICRO_FIELDS = ("qps", "p95_latency_ms")


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def positive_number(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def add_metric(results, group, name, field, value):
    definition = FIELD_DEFS.get(field)
    if definition is None:
        return
    number = positive_number(value)
    if number is None:
        print(f"[AGGREGATE] skipping non-positive/invalid metric {name!r}={value!r}")
        return
    results[name] = {
        "source_name": name,
        "source_field": field,
        "group": group,
        "value": number,
        "unit": definition["unit"],
        "direction": definition["direction"],
    }


def collect_primary(primary, results):
    summary = primary.get("results_summary")
    if not isinstance(summary, dict):
        return
    for workload, thread_data in summary.items():
        if not isinstance(thread_data, dict):
            continue
        for label, metrics in thread_data.items():
            if not isinstance(metrics, dict):
                continue
            for field in FIELD_DEFS:
                name = f"{workload}: {label} {field}"
                add_metric(results, workload, name, field, metrics.get(field))


def collect_micro(micro, results):
    data = micro.get("results")
    if not isinstance(data, dict):
        return
    for group_name, key in (("thread_scaling", "thread_scaling"),
                            ("table_count_sweep", "table_count_sweep")):
        sweep = data.get(key)
        if not isinstance(sweep, dict):
            continue
        for label, metrics in sweep.items():
            if not isinstance(metrics, dict):
                continue
            for field in MICRO_FIELDS:
                name = f"{group_name}: {label} {field}"
                add_metric(results, group_name, name, field, metrics.get(field))


def aggregate_results(results_dir, output_file):
    primary = load_json(os.path.join(results_dir, "benchmark_ob.json"))
    micro = load_json(os.path.join(results_dir, "micro_benchmark.json"))

    results = {}
    collect_primary(primary, results)
    collect_micro(micro, results)

    if not results:
        raise RuntimeError("no valid OceanBase benchmark metrics were collected")

    software = os.environ.get("SOFTWARE_NAME", "oceanbase")
    version = os.environ.get("SOFTWARE_VERSION", "5.0.1.0")
    architecture = os.environ.get("EXPECTED_ARCH", "x86_64")

    payload = {
        "benchmark": "oceanbase_oltp_sysbench",
        "software": software,
        "version": version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "primary": primary.get("parameters", {}),
            "micro": micro.get("parameters", {}),
        },
        "results": results,
    }

    os.makedirs(os.path.dirname(os.path.abspath(output_file)) or ".", exist_ok=True)
    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[AGGREGATE] collected {len(results)} metrics into {output_file}")
    return payload


def main():
    if len(sys.argv) < 3:
        print("Usage: aggregate_results.py <results_dir> <output_file>")
        return 2
    try:
        aggregate_results(sys.argv[1], sys.argv[2])
    except RuntimeError as exc:
        print(f"[AGGREGATE] ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())