#!/usr/bin/env python3
"""Run and normalize the two local Envoy HTTPS benchmark scenarios."""

from __future__ import annotations

import argparse
import json
import math
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

VUS = 30
DURATION_SECONDS = 20
SCENARIOS = (
    ("direct_response", "HTTPS direct response", "direct_url"),
    ("reverse_proxy", "HTTPS reverse proxy", "reverse_url"),
)

SCRIPT = """import http from 'k6/http';

export const options = {
  insecureSkipTLSVerify: true,
  noConnectionReuse: true,
  noVUConnectionReuse: true,
};

export default function () {
  http.get(__ENV.ENVOY_TARGET);
}
"""


def value(metrics: dict[str, Any], metric: str, field: str, scenario: str) -> float:
    entry = metrics.get(metric)
    if not isinstance(entry, dict):
        raise TypeError(f"{scenario}: k6 summary is missing {metric}")
    values = entry.get("values", entry)
    if not isinstance(values, dict):
        raise TypeError(f"{scenario}: k6 summary has invalid {metric} values")
    result = values.get(field)
    if result is None and field == "rate":
        result = values.get("value")
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise TypeError(f"{scenario}: k6 summary is missing {metric}.{field}")
    result = float(result)
    if not math.isfinite(result) or result < 0:
        raise RuntimeError(f"{scenario}: k6 summary has invalid {metric}.{field}")
    return result


def parse_summary(path: Path, scenario: str, group: str) -> dict[str, dict[str, Any]]:
    try:
        summary = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{scenario}: cannot read k6 summary: {exc}") from exc
    metrics = summary.get("metrics")
    if not isinstance(metrics, dict):
        raise TypeError(f"{scenario}: k6 summary is missing metrics")

    request_count = value(metrics, "http_reqs", "count", scenario)
    request_rate = value(metrics, "http_reqs", "rate", scenario)
    average_latency = value(metrics, "http_req_duration", "avg", scenario)
    p95_latency = value(metrics, "http_req_duration", "p(95)", scenario)
    failed_rate = value(metrics, "http_req_failed", "rate", scenario)
    if request_count <= 0 or request_rate <= 0:
        raise RuntimeError(f"{scenario}: k6 produced no successful HTTP request throughput")
    if failed_rate != 0:
        raise RuntimeError(f"{scenario}: k6 reported a non-zero http_req_failed rate: {failed_rate}")

    extracted = {
        "http_reqs.count": (request_count, "requests", "higher_is_better"),
        "http_reqs.rate": (request_rate, "requests/s", "higher_is_better"),
        "http_req_duration.avg": (average_latency, "ms", "lower_is_better"),
        "http_req_duration.p(95)": (p95_latency, "ms", "lower_is_better"),
    }
    return {
        f"{scenario} :: {field}": {
            "source_name": f"{scenario} :: {field}",
            "source_field": field,
            "value": result,
            "unit": unit,
            "direction": direction,
            "group": group,
        }
        for field, (result, unit, direction) in extracted.items()
    }


def run_scenario(k6: Path, script: Path, name: str, url: str, raw: Any) -> Path:
    summary = script.parent / f"benchmark_{name}_summary.json"
    command = [
        str(k6),
        "run",
        "--vus",
        str(VUS),
        "--duration",
        f"{DURATION_SECONDS}s",
        "--summary-export",
        str(summary),
        str(script),
    ]
    raw.write(f"[envoy-k6] scenario: {name}\n")
    raw.write("[envoy-k6] command: " + " ".join(command) + "\n")
    completed = subprocess.run(
        command,
        env={**os.environ, "ENVOY_TARGET": url},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    raw.write(completed.stdout)
    raw.write("\n")
    if completed.returncode != 0:
        raise RuntimeError(f"{name}: k6 exited with code {completed.returncode}")
    if not summary.is_file():
        raise RuntimeError(f"{name}: k6 did not write its summary JSON")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--k6", type=Path, required=True)
    parser.add_argument("--direct-url", required=True)
    parser.add_argument("--reverse-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--raw-output", type=Path, required=True)
    args = parser.parse_args()

    if not args.k6.is_file() or not (args.k6.stat().st_mode & 0o111):
        print("[envoy-k6] ERROR: k6 executable is unavailable", file=sys.stderr)
        return 1
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.raw_output.parent.mkdir(parents=True, exist_ok=True)
    script = args.output.parent / "envoy_k6_request.js"
    script.write_text(SCRIPT, encoding="utf-8")

    try:
        results: dict[str, dict[str, Any]] = {}
        with args.raw_output.open("w", encoding="utf-8") as raw:
            for name, group, argument_name in SCENARIOS:
                url = getattr(args, argument_name)
                summary = run_scenario(args.k6, script, name, url, raw)
                results.update(parse_summary(summary, name, group))
        if not results:
            raise RuntimeError("k6 produced no Envoy metrics")
    except (RuntimeError, TypeError) as exc:
        print(f"[envoy-k6] ERROR: {exc}", file=sys.stderr)
        return 1

    payload = {
        "benchmark": "envoy_single_machine_https",
        "software": "envoy",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "tool": "k6",
            "vus": VUS,
            "duration_seconds": DURATION_SECONDS,
            "connection_reuse": False,
            "scenarios": {
                "direct_response": "TLS listener with Envoy direct_response 200",
                "reverse_proxy": "TLS listener forwarding to a local Envoy backend",
            },
        },
        "metric_contract": {
            "scope": "k6 summary fields for each declared HTTPS scenario",
            "source": "http_reqs and http_req_duration",
            "failure_policy": "http_req_failed.rate must be zero",
        },
        "results": results,
    }
    args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[envoy-k6] normalized {len(results)} scenario metrics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
