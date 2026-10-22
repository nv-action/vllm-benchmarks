#!/usr/bin/env python3
"""Normalize the upstream rdma_performance TCP client's eight message sizes."""

from __future__ import annotations

import json
import math
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ATTACHMENT_SIZES = (0, 1024, 4096, 8192, 102400, 204800, 1048576, 8388608)
REPETITIONS = 5
MAX_INFLIGHT_ATTACHMENT_BYTES = 32 * 1024 * 1024
MAX_CLIENT_THREADS = 32
MAX_QUEUE_DEPTH = 32
NUMBER = r"([0-9]+(?:\.[0-9]+)?)"
SCENARIO = re.compile(
    r"\[Threads:\s*(\d+),\s*Depth:\s*(\d+),\s*Attachment:\s*(\d+)B,\s*"
    r"RDMA:\s*no,\s*Echo:\s*yes\]"
)
SUMMARY_FIELDS = {
    "avg_latency": (r"Avg-Latency:\s*" + NUMBER, "us", "lower_is_better"),
    "p90_latency": (r"90th-Latency:\s*" + NUMBER, "us", "lower_is_better"),
    "p99_latency": (r"99th-Latency:\s*" + NUMBER, "us", "lower_is_better"),
    "p999_latency": (r"99\.9th-Latency:\s*" + NUMBER, "us", "lower_is_better"),
    "throughput": (r"Throughput:\s*" + NUMBER + r"MB/s", "MB/s", "higher_is_better"),
    "qps": (r"QPS:\s*" + NUMBER + r"k(?:\s|,|$)", "requests/s", "higher_is_better"),
}


def client_workload(attachment_size: int) -> tuple[int, int]:
    if attachment_size == 0:
        return MAX_CLIENT_THREADS, MAX_QUEUE_DEPTH
    max_inflight = MAX_INFLIGHT_ATTACHMENT_BYTES // attachment_size
    threads = min(MAX_CLIENT_THREADS, max_inflight)
    depth = min(MAX_QUEUE_DEPTH, max_inflight // threads)
    return threads, depth


def parse_summary(line: str, attachment_size: int) -> dict[str, float]:
    values = {}
    for name, (pattern, _, _) in SUMMARY_FIELDS.items():
        matches = re.findall(pattern, line)
        if len(matches) != 1:
            raise ValueError(f"missing or ambiguous official client field: {name}")
        value = float(matches[0])
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"invalid official client field: {name}={value}")
        values[name] = value
    # Upstream prints QPS using integer division in thousands of requests/s.
    # Large attachments can therefore show 0k. Its throughput is calculated
    # from completed request-attachment bytes and retains fractional precision.
    if attachment_size:
        values["qps"] = values["throughput"] * 1048576 / attachment_size
    else:
        values["qps"] *= 1000
    if values["qps"] <= 0:
        raise ValueError(f"the {attachment_size}B scenario has no measurable QPS")
    return values


def parse_client_log(text: str) -> dict[str, dict[str, object]]:
    if "RPC call failed:" in text:
        raise ValueError("the official client reported a failed RPC")
    scenarios: dict[int, list[dict[str, float]]] = {size: [] for size in ATTACHMENT_SIZES}
    current_size = None
    for line in text.splitlines():
        match = SCENARIO.search(line)
        if match:
            threads, depth, current_size = map(int, match.groups())
            if current_size not in scenarios:
                raise ValueError(f"unexpected attachment size: {current_size}B")
            expected_threads, expected_depth = client_workload(current_size)
            if (threads, depth) != (expected_threads, expected_depth):
                raise ValueError(
                    f"{current_size}B requires {expected_threads} threads and depth "
                    f"{expected_depth}, found {threads} threads and depth {depth}"
                )
            continue
        if "Avg-Latency:" not in line:
            continue
        if current_size is None:
            raise ValueError("client summary has no preceding TCP echo scenario")
        scenarios[current_size].append(parse_summary(line, current_size))
        current_size = None

    results: dict[str, dict[str, object]] = {}
    for attachment_size, repetitions in scenarios.items():
        if len(repetitions) != REPETITIONS:
            raise ValueError(
                f"{attachment_size}B requires {REPETITIONS} client summaries, found {len(repetitions)}"
            )
        group = f"{attachment_size}B attachment"
        for name, (_, unit, direction) in SUMMARY_FIELDS.items():
            samples = [repetition[name] for repetition in repetitions]
            key = f"attachment_{attachment_size}B_{name}"
            results[key] = {
                "source_name": key,
                "source_field": "client_stdout.Throughput" if name == "qps" and attachment_size else "client_stdout",
                "group": group,
                "attachment_size": attachment_size,
                "client_threads": client_workload(attachment_size)[0],
                "queue_depth": client_workload(attachment_size)[1],
                "measurement": name,
                "samples": samples,
                "repetitions": REPETITIONS,
                "value": sum(samples) / REPETITIONS,
                "unit": unit,
                "direction": direction,
            }
    return results


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: parse_benchmark.py CLIENT_LOG OUTPUT_JSON", file=sys.stderr)
        return 1
    try:
        results = parse_client_log(Path(sys.argv[1]).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"[brpc-parse] ERROR: {exc}", file=sys.stderr)
        return 1
    output = {
        "benchmark": "brpc_upstream_rdma_performance_tcp",
        "software": "brpc",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "official_entry": "example/rdma_performance/client.cpp",
            "official_server": "example/rdma_performance/server.cpp",
            "transport": "TCP (--use_rdma=false)",
            "protocol": "baidu_std",
            "connection_type": "single",
            "server": "127.0.0.1:8003",
            "server_bthread_concurrency": 32,
            "max_inflight_attachment_bytes": MAX_INFLIGHT_ATTACHMENT_BYTES,
            "client_workloads": {
                str(size): {"threads": client_workload(size)[0], "queue_depth": client_workload(size)[1]}
                for size in ATTACHMENT_SIZES
            },
            "client_bthread_concurrency": 160,
            "attachment_sizes": list(ATTACHMENT_SIZES),
            "repetitions": REPETITIONS,
            "echo_attachment": True,
            "test_seconds_per_run": 20,
        },
        "results": results,
    }
    Path(sys.argv[2]).write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(f"[brpc-parse] normalized {len(results)} measurements from eight sizes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
