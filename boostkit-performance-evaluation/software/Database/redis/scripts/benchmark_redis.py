#!/usr/bin/env python3
"""Measure SET and GET throughput at three client counts on one Redis instance."""

from __future__ import annotations

import json
import math
import os
import pty
import re
import select
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REQUEST_COUNT = 10_000_000
CLIENT_COUNTS = (40, 60, 80)
OPERATIONS = ("SET", "GET")
PAYLOAD_BYTES = 3
KEYSPACE_LENGTH = 10_000_000
THREAD_COUNT = 20
COMMAND_TIMEOUT_SECONDS = 14_400
CLIENT_HEADER = re.compile(r"(?m)^# clients=(\d+)$")
THROUGHPUT = re.compile(
    r"(?m)^([A-Z][A-Z0-9_]*):\s*([0-9]+(?:\.[0-9]+)?)\s+requests per second\b"
)


def benchmark_command(binary: Path, port: int, clients: int) -> list[str]:
    return [
        str(binary),
        "-h",
        "127.0.0.1",
        "-p",
        str(port),
        "-c",
        str(clients),
        "-d",
        str(PAYLOAD_BYTES),
        "-n",
        str(REQUEST_COUNT),
        "-r",
        str(KEYSPACE_LENGTH),
        "-t",
        "set,get",
        "--threads",
        str(THREAD_COUNT),
        "-q",
    ]


def parse_throughput_results(output: str) -> dict[str, dict[str, Any]]:
    output = output.replace("\r\n", "\n").replace("\r", "\n")
    headers = list(CLIENT_HEADER.finditer(output))
    if [int(header.group(1)) for header in headers] != list(CLIENT_COUNTS):
        raise RuntimeError(
            "redis-benchmark output must contain exactly the 40, 60, 80 client runs"
        )
    results: dict[str, dict[str, Any]] = {}
    for index, header in enumerate(headers):
        clients = int(header.group(1))
        section_end = headers[index + 1].start() if index + 1 < len(headers) else len(output)
        section = output[header.end():section_end]
        measurements = THROUGHPUT.findall(section)
        if [operation for operation, _ in measurements] != list(OPERATIONS):
            raise RuntimeError(
                f"{clients} clients requires exactly one SET and one GET throughput"
            )
        for operation, raw_value in measurements:
            value = float(raw_value)
            if not math.isfinite(value) or value <= 0:
                raise RuntimeError(
                    f"{clients} clients {operation} has invalid throughput: {raw_value}"
                )
            key = f"clients_{clients}_{operation.lower()}"
            results[key] = {
                "source_name": f"{clients} clients {operation}: requests per second",
                "source_field": f"{operation}: <value> requests per second",
                "group": f"{clients} clients",
                "clients": clients,
                "operation": operation,
                "value": value,
                "unit": "requests/s",
                "direction": "higher_is_better",
            }
    return results


def run_benchmark(binary: Path, port: int, clients: int, raw_output: Path, deadline: float) -> None:
    command = benchmark_command(binary, port, clients)
    print(f"[redis-benchmark] {' '.join(command)}", flush=True)
    master_fd, slave_fd = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        # Keep completed SET/GET output visible in the live log and raw file.
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=slave_fd,
            stderr=slave_fd,
            close_fds=True,
        )
    finally:
        os.close(slave_fd)

    with raw_output.open("ab") as raw_file:
        raw_file.write(f"# clients={clients}\n".encode("utf-8"))
        raw_file.write(f"$ {' '.join(command)}\n".encode("utf-8"))
        try:
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    process.kill()
                    process.wait()
                    raise RuntimeError(
                        f"redis-benchmark suite exceeded {COMMAND_TIMEOUT_SECONDS} seconds"
                    )
                readable, _, _ = select.select([master_fd], [], [], min(1.0, remaining))
                if not readable:
                    continue
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    continue
                raw_file.write(chunk)
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()

            while True:
                readable, _, _ = select.select([master_fd], [], [], 0)
                if not readable:
                    break
                try:
                    chunk = os.read(master_fd, 65_536)
                except OSError:
                    break
                if not chunk:
                    break
                raw_file.write(chunk)
                sys.stdout.write(chunk.decode("utf-8", errors="replace"))
                sys.stdout.flush()
        finally:
            os.close(master_fd)

    if process.returncode:
        raise RuntimeError(
            f"redis-benchmark with {clients} clients exited with code {process.returncode}"
        )


def main() -> int:
    if len(sys.argv) != 4:
        print(
            "usage: benchmark_redis.py REDIS_BENCHMARK RAW_OUTPUT NORMALIZED_OUTPUT",
            file=sys.stderr,
        )
        return 1

    benchmark_binary, raw_output, normalized_output = map(Path, sys.argv[1:])
    if not benchmark_binary.is_file() or not os.access(benchmark_binary, os.X_OK):
        raise RuntimeError(f"redis-benchmark executable is unavailable: {benchmark_binary}")
    redis_cli = benchmark_binary.with_name("redis-cli")
    if not redis_cli.is_file() or not os.access(redis_cli, os.X_OK):
        raise RuntimeError(f"redis-cli executable is unavailable: {redis_cli}")

    port = int(os.environ["REDIS_SERVICE_PORT"])
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_bytes(b"")
    deadline = time.monotonic() + COMMAND_TIMEOUT_SECONDS
    for clients in CLIENT_COUNTS:
        cleared = subprocess.run(
            [str(redis_cli), "-h", "127.0.0.1", "-p", str(port), "FLUSHALL"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if cleared.returncode != 0 or cleared.stdout.strip() != "OK":
            raise RuntimeError(
                f"failed to clear the private Redis instance before {clients} clients"
            )
        run_benchmark(benchmark_binary, port, clients, raw_output, deadline)
    output = raw_output.read_text(encoding="utf-8", errors="replace")
    results = parse_throughput_results(output)

    payload = {
        "benchmark": "redis_single_instance_set_get_throughput",
        "software": "redis",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "client_host": "127.0.0.1",
            "operations": list(OPERATIONS),
            "requests": REQUEST_COUNT,
            "client_counts": list(CLIENT_COUNTS),
            "payload_bytes": PAYLOAD_BYTES,
            "keyspace_length": KEYSPACE_LENGTH,
            "threads": THREAD_COUNT,
            "metric_source": "<operation>: <value> requests per second",
        },
        "results": results,
    }
    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"[redis-benchmark] normalized {len(results)} SET/GET throughput metrics")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
