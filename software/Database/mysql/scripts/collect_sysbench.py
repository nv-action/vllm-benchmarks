#!/usr/bin/env python3
"""Normalize the official Sysbench summaries from the MySQL OLTP matrix."""

import argparse
import json
import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path


RUN_NAME = re.compile(r"run-(read_only|read_write|write_only)-threads(\d+)\.log")
TRANSACTIONS = re.compile(r"^\s*transactions:\s+\d+\s+\(([0-9.]+) per sec\.\)", re.M)
QUERIES = re.compile(r"^\s*queries:\s+\d+\s+\(([0-9.]+) per sec\.\)", re.M)
P95 = re.compile(r"^\s*95th percentile:\s+([0-9.]+)\s*$", re.M)
FIELDS = (
    ("TPS", TRANSACTIONS, "transactions/s", "higher_is_better"),
    ("QPS", QUERIES, "queries/s", "higher_is_better"),
    ("P95 latency", P95, "ms", "lower_is_better"),
)


def collect(runs_dir: Path) -> dict[str, dict]:
    expected = {
        (scenario, threads)
        for scenario in ("read_only", "read_write", "write_only")
        for threads in (1, 32, 128, 256)
    }
    found = set()
    results = {}
    for path in sorted(runs_dir.glob("run-*.log")):
        match = RUN_NAME.fullmatch(path.name)
        if match is None:
            raise ValueError(f"unexpected Sysbench run file: {path}")
        scenario, threads_text = match.groups()
        threads = int(threads_text)
        key = (scenario, threads)
        if key not in expected or key in found:
            raise ValueError(f"unexpected or duplicate Sysbench scenario: {path}")
        found.add(key)
        output = path.read_text(encoding="utf-8", errors="replace")
        for field, pattern, unit, direction in FIELDS:
            values = pattern.findall(output)
            if len(values) != 1:
                raise ValueError(f"expected one {field} summary in {path}, got {len(values)}")
            value = float(values[0])
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"invalid {field} in {path}: {value}")
            source_name = f"sysbench {scenario} --threads={threads}: {field}"
            results[source_name] = {
                "source_name": source_name,
                "scenario": scenario,
                "threads": threads,
                "source_field": field,
                "value": value,
                "unit": unit,
                "direction": direction,
                "source_file": str(path.relative_to(runs_dir.parent)),
            }
    if found != expected:
        missing = sorted(expected - found)
        raise ValueError(f"missing {len(missing)} Sysbench runs: {missing[:5]}")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs_dir", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    try:
        results = collect(args.runs_dir)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"[mysql] ERROR: {exc}\n")
    args.output.write_text(json.dumps({
        "software": "mysql",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "results": results,
    }, indent=2) + "\n", encoding="utf-8")
    print(f"[mysql] normalized {len(results)} Sysbench measurements")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
