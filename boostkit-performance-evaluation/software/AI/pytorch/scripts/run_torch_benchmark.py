#!/usr/bin/env python3
"""Run the official torch.utils.benchmark operator suite for PyTorch CPU.

Each operator is measured with torch.utils.benchmark.Timer at every declared
thread level; the per-run median is kept as the reported metric.  Operators
with a declared work quantity (FLOPs or bytes) additionally report the derived
throughput.  No cross-operator aggregation is performed.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from datetime import datetime, timezone
from typing import Any

import torch
from torch.utils.benchmark import Timer

# name, group, setup, stmt, work (FLOPs or bytes), work kind
OPERATORS: list[dict[str, Any]] = [
    {
        "name": "matmul_square_512",
        "group": "matmul",
        "setup": "a = torch.randn(512, 512); b = torch.randn(512, 512)",
        "stmt": "torch.matmul(a, b)",
        "work": 2 * 512**3,
        "kind": "flops",
    },
    {
        "name": "matmul_square_1024",
        "group": "matmul",
        "setup": "a = torch.randn(1024, 1024); b = torch.randn(1024, 1024)",
        "stmt": "torch.matmul(a, b)",
        "work": 2 * 1024**3,
        "kind": "flops",
    },
    {
        "name": "matmul_square_2048",
        "group": "matmul",
        "setup": "a = torch.randn(2048, 2048); b = torch.randn(2048, 2048)",
        "stmt": "torch.matmul(a, b)",
        "work": 2 * 2048**3,
        "kind": "flops",
    },
    {
        "name": "conv2d_3x3_64x56",
        "group": "conv",
        "setup": (
            "x = torch.randn(1, 64, 56, 56); "
            "w = torch.randn(64, 64, 3, 3); "
            "c = torch.nn.functional.conv2d"
        ),
        "stmt": "c(x, w)",
        "work": 2 * 64 * 56 * 56 * 64 * 3 * 3,
        "kind": "flops",
    },
    {
        "name": "elementwise_add_32m",
        "group": "elementwise",
        "setup": "a = torch.randn(1 << 23); b = torch.randn(1 << 23)",
        "stmt": "a + b",
        "work": 3 * 4 * (1 << 23),
        "kind": "bytes",
    },
    {
        "name": "reduction_sum_64m",
        "group": "reduction",
        "setup": "a = torch.randn(1 << 24)",
        "stmt": "torch.sum(a)",
        "work": 4 * (1 << 24),
        "kind": "bytes",
    },
]

DERIVED_UNITS = {
    "flops": ("GFLOP/s", "higher_is_better"),
    "bytes": ("GB/s", "higher_is_better"),
}


def parse_threads(raw: str) -> list[int]:
    levels = [int(item) for item in raw.replace(",", " ").split()]
    if not levels or any(level < 1 for level in levels):
        raise ValueError(f"invalid thread levels: {raw!r}")
    return levels


def record(results: dict[str, dict[str, Any]], name: str, group: str,
           field: str, value: float, unit: str, direction: str) -> None:
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError(f"metric {name} is not positive and finite: {value}")
    if name in results:
        raise RuntimeError(f"duplicate metric: {name}")
    results[name] = {
        "source_name": name,
        "source_field": field,
        "group": group,
        "value": round(value, 4),
        "unit": unit,
        "direction": direction,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threads", default=os.environ.get("TORCH_THREAD_LEVELS", "1 4 16"))
    parser.add_argument("--results", required=True)
    parser.add_argument("--min-run-time", type=float, default=0.5)
    args = parser.parse_args()

    thread_levels = parse_threads(args.threads)
    software_version = os.environ.get("SOFTWARE_VERSION", "2.13.0")
    architecture = os.environ.get("EXPECTED_ARCH", "x86_64")

    torch.manual_seed(0)
    try:
        torch.set_num_interop_threads(1)
    except RuntimeError:
        pass  # already initialized; inter-op threads stay at their default

    print(f"[torch] torch={torch.__version__} arch={architecture} "
          f"threads={thread_levels} "
          f"capabilities={torch.backends.cpu.get_cpu_capability()}")

    results: dict[str, dict[str, Any]] = {}
    for threads in thread_levels:
        torch.set_num_threads(threads)
        for op in OPERATORS:
            timer = Timer(stmt=op["stmt"], setup=op["setup"])
            measurement = timer.blocked_autorange(min_run_time=args.min_run_time)
            median_seconds = measurement.median
            median_ms = median_seconds * 1000.0
            name = f"torch {op['name']} --threads={threads}: median_time_ms"
            record(results, name, op["group"], "median_time_ms", median_ms, "ms",
                   "lower_is_better")
            line = (f"[torch] {op['name']} --threads={threads}: "
                    f"median_time_ms={results[name]['value']}")
            if op.get("work"):
                unit, direction = DERIVED_UNITS[op["kind"]]
                derived = op["work"] / median_seconds / 1e9
                derived_name = f"torch {op['name']} --threads={threads}: {unit}"
                record(results, derived_name, op["group"], unit, derived, unit,
                       direction)
                line += f", {unit}={results[derived_name]['value']}"
            print(line, flush=True)

    payload = {
        "benchmark": "torch_utils_benchmark",
        "software": "pytorch",
        "version": software_version,
        "architecture": architecture,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "torch_version": torch.__version__,
            "repository": os.environ.get("PYTORCH_REPOSITORY",
                                         "https://github.com/pytorch/pytorch"),
            "operators": [op["name"] for op in OPERATORS],
            "thread_levels": thread_levels,
            "min_run_time": args.min_run_time,
        },
        "results": results,
    }
    output = os.path.abspath(args.results)
    os.makedirs(os.path.dirname(output), exist_ok=True)
    temporary = f"{output}.tmp"
    with open(temporary, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, output)
    print(f"[torch] recorded {len(results)} metrics into {output}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"[torch] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)
