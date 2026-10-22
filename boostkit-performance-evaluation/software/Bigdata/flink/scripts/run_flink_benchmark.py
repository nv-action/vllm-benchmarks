#!/usr/bin/env python3
"""Submit the finite Flink smoke benchmark and collect its REST evidence."""

from __future__ import annotations

import argparse
import json
import math
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import urlopen


JOB_ID_PATTERN = re.compile(r"Job has been submitted with JobID ([0-9a-fA-F]+)")


def request_json(base_url: str, path: str, query: dict[str, str] | None = None) -> object:
    url = f"{base_url.rstrip('/')}{path}"
    if query:
        url = f"{url}?{urlencode(query)}"
    with urlopen(url, timeout=10) as response:  # nosec B310: localhost REST endpoint
        return json.loads(response.read().decode("utf-8"))


def wait_for_job(base_url: str, job_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            payload = request_json(base_url, f"/jobs/{job_id}")
        except (URLError, TimeoutError, json.JSONDecodeError):
            time.sleep(1)
            continue
        if not isinstance(payload, dict):
            raise RuntimeError("Flink REST job response is not an object")
        state = payload.get("state")
        if state == "FINISHED":
            return payload
        if state in {"FAILED", "CANCELED", "SUSPENDED"}:
            raise RuntimeError(f"Flink job finished unsuccessfully: {state}")
        time.sleep(1)
    raise RuntimeError("Flink job did not finish within 120 seconds")


def sink_records(base_url: str, job: dict[str, object]) -> float:
    vertices = job.get("vertices")
    if not isinstance(vertices, list):
        raise RuntimeError("Flink REST job response has no vertices")
    sinks = [
        vertex
        for vertex in vertices
        if isinstance(vertex, dict)
        and isinstance(vertex.get("name"), str)
        and "Discard sink" in vertex["name"]
    ]
    if len(sinks) != 1:
        raise RuntimeError("Flink job must contain exactly one vertex containing Discard sink")
    vertex_id = sinks[0].get("id")
    if not isinstance(vertex_id, str) or not vertex_id:
        raise RuntimeError("Discard sink vertex is missing its id")
    job_id = job.get("jid")
    if not isinstance(job_id, str) or not job_id:
        raise RuntimeError("Flink REST job response is missing its job id")
    payload = request_json(
        base_url,
        f"/jobs/{job_id}/vertices/{vertex_id}/metrics",
        {"get": "numRecordsIn"},
    )
    if not isinstance(payload, list) or len(payload) != 1:
        raise RuntimeError("Discard sink numRecordsIn metric is unavailable")
    metric = payload[0]
    if not isinstance(metric, dict) or metric.get("id") != "numRecordsIn":
        raise RuntimeError("Discard sink returned an unexpected metric")
    try:
        value = float(metric["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Discard sink numRecordsIn is not numeric") from exc
    if not math.isfinite(value) or value <= 0:
        raise RuntimeError("Discard sink numRecordsIn must be positive and finite")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--flink", required=True, type=Path)
    parser.add_argument("--job-jar", required=True, type=Path)
    parser.add_argument("--rest-url", required=True)
    parser.add_argument("--records", required=True, type=int)
    parser.add_argument("--parallelism", required=True, type=int)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    command = [
        str(args.flink),
        "run",
        "-d",
        "-c",
        "org.boostkit.performance.flink.PassThroughJob",
        str(args.job_jar),
        "--records",
        str(args.records),
        "--parallelism",
        str(args.parallelism),
    ]
    print(f"[flink-benchmark] {' '.join(command)}", flush=True)
    completed = subprocess.run(command, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    print(completed.stdout, end="", flush=True)
    if completed.returncode != 0:
        raise RuntimeError(f"Flink job submission failed with code {completed.returncode}")
    match = JOB_ID_PATTERN.search(completed.stdout)
    if match is None:
        raise RuntimeError("Flink CLI output does not contain a submitted job id")

    job = wait_for_job(args.rest_url, match.group(1))
    start_time = job.get("start-time")
    end_time = job.get("end-time")
    if isinstance(start_time, bool) or not isinstance(start_time, (int, float)):
        raise RuntimeError("Flink job start-time is invalid")
    if isinstance(end_time, bool) or not isinstance(end_time, (int, float)):
        raise RuntimeError("Flink job end-time is invalid")
    duration_seconds = (float(end_time) - float(start_time)) / 1000.0
    if not math.isfinite(duration_seconds) or duration_seconds <= 0:
        raise RuntimeError("Flink job duration must be positive and finite")
    processed_records = sink_records(args.rest_url, job)
    if processed_records != args.records:
        raise RuntimeError(
            f"Discard sink processed {processed_records:g} records, expected {args.records}"
        )
    throughput = processed_records / duration_seconds
    payload = {
        "benchmark": "flink_standalone_stateless_pass_through",
        "parameters": {
            "records": args.records,
            "parallelism": args.parallelism,
            "state_backend": "none",
            "checkpointing": "disabled",
        },
        "job": job,
        "results": {
            "discard_sink_num_records_in_per_second": {
                "source_name": "Discard sink numRecordsIn per second",
                "value": throughput,
                "unit": "records/s",
                "direction": "higher_is_better",
                "group": "无状态直通流",
                "raw_metric": "numRecordsIn",
                "raw_value": processed_records,
                "job_duration_seconds": duration_seconds,
            }
        },
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimeError as exc:
        print(f"[flink-benchmark] ERROR: {exc}", file=sys.stderr)
        raise SystemExit(50)
