#!/usr/bin/env python3
"""Normalize the four BiSheng JDK FloatingScalarVectorAbsDiff JMH scores."""

from __future__ import annotations

import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


METHODS = (
    "testVectorAbsDiffFloat",
    "testVectorAbsDiffDouble",
    "testScalarAbsDiffFloat",
    "testScalarAbsDiffDouble",
)


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: parse_benchmark.py JMH_JSON NORMALIZED_JSON", file=sys.stderr)
        return 1

    source, destination = map(Path, sys.argv[1:])
    try:
        benchmark = os.environ["JMH_BENCHMARK"]
        count = int(os.environ["JMH_COUNT"])
        records = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(records, list):
            raise ValueError("JMH JSON root must be an array")

        results = {}
        for record in records:
            if not isinstance(record, dict):
                raise ValueError("JMH result must be an object")
            name = record.get("benchmark")
            if not isinstance(name, str) or not name.startswith(benchmark + "."):
                raise ValueError(f"unexpected JMH benchmark: {name}")
            method = name.removeprefix(benchmark + ".")
            if method not in METHODS or name in results:
                raise ValueError(f"unexpected or duplicate JMH method: {name}")
            if record.get("mode") != "avgt" or record.get("threads") != 1:
                raise ValueError(f"{name}: expected AverageTime with one thread")
            if record.get("forks") != 3 or record.get("warmupIterations") != 4 or \
                    record.get("measurementIterations") != 4:
                raise ValueError(f"{name}: unexpected fork or iteration count")
            params = record.get("params")
            if not isinstance(params, dict) or params.get("count") != str(count):
                raise ValueError(f"{name}: expected count={count}")
            metric = record.get("primaryMetric")
            if not isinstance(metric, dict) or metric.get("scoreUnit") != "ns/op":
                raise ValueError(f"{name}: expected an ns/op primary metric")
            score = metric.get("score")
            if isinstance(score, bool) or not isinstance(score, (int, float)):
                raise ValueError(f"{name}: JMH score is not numeric")
            if not math.isfinite(score) or score <= 0:
                raise ValueError(f"{name}: JMH score must be positive and finite")
            results[name] = {
                "source_name": name,
                "source_field": "primaryMetric.score",
                "value": score,
                "unit": "ns/op",
                "source_file": source.name,
                "score_error": metric.get("scoreError"),
                "params": record.get("params"),
            }

        expected = {f"{benchmark}.{method}" for method in METHODS}
        if set(results) != expected:
            raise ValueError(f"JMH result is incomplete: missing {sorted(expected - set(results))}")

        normalized = {
            "benchmark": "bishengjdk_floating_scalar_vector_abs_diff",
            "software": "bishengjdk",
            "version": os.environ["SOFTWARE_VERSION"],
            "architecture": os.environ["EXPECTED_ARCH"],
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "parameters": {
                "source_repository": "https://gitee.com/openeuler/bishengjdk-21",
                "benchmark_class": benchmark,
                "jmh_version": os.environ["JMH_VERSION"],
                "count": count,
                "threads": 1,
                "forks": 3,
                "warmup_iterations": 4,
                "warmup_seconds": 2,
                "measurement_iterations": 4,
                "measurement_seconds": 2,
                "cpu_affinity": "none",
                "command": [
                    "make", "test", f"CONF={os.environ['BUILD_CONF']}",
                    f"TEST=micro:{benchmark}",
                    f"MICRO=FORK=3;WARMUP_ITER=4;WARMUP_TIME=2;ITER=4;TIME=2;"
                    f"RESULTS_FORMAT=json;OPTIONS=-t 1 -p count={count}",
                ],
            },
            "metric_contract": {
                "source_field": "primaryMetric.score",
                "unit": "ns/op",
                "direction": "lower_is_better",
            },
            "results": results,
        }
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(
            json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except (KeyError, OSError, ValueError, TypeError) as exc:
        print(f"[bishengjdk-parse] ERROR: {exc}", file=sys.stderr)
        return 1

    print(f"[bishengjdk-parse] normalized {len(results)} official JMH ns/op metrics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
