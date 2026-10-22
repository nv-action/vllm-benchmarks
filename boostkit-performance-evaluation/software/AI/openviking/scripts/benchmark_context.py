#!/usr/bin/env python3
"""Run the OpenViking AGFS context filesystem benchmark.

Ports the upstream openEuler reference scheme: the AGFS (RAGFS) binding client
from openviking.pyagfs mounts an in-memory "memfs" filesystem and measures
write/read/stat/ls/rm/mkdir/grep operations across 1KB/64KB/1MB payloads.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import time


SIZE_CONFIGS = (
    ("small_1kb", 1024),
    ("medium_64kb", 65536),
    ("large_1mb", 1048576),
)
OPERATIONS = ("write", "read", "stat", "ls", "rm", "mkdir", "grep")


def get_binding_client():
    from openviking.pyagfs import get_binding_client as _get_binding_client

    return _get_binding_client()


def timed(fn, *args, **kwargs):
    start = time.perf_counter()
    fn(*args, **kwargs)
    return (time.perf_counter() - start) * 1000.0


def stats_ms(latencies):
    if not latencies:
        return 0.0, 0.0
    total = sum(latencies)
    avg = total / len(latencies)
    ops = len(latencies) * 1000.0 / total if total > 0 else 0.0
    return round(avg, 3), round(ops, 2)


def run_context_benchmark(output_file, iterations):
    import openviking

    software_version = openviking.__version__
    architecture = platform.machine()
    print(f"[context_fs] openviking={software_version} arch={architecture} "
          f"iterations={iterations}")

    BindingClient, _FileHandle = get_binding_client()
    client = BindingClient()
    mount_path = "viking://context-bench/"
    client.mount("memfs", mount_path)
    print(f"[context_fs] mounted memfs at {mount_path}")
    try:
        results_summary = {}
        for size_name, data_size in SIZE_CONFIGS:
            payload = os.urandom(data_size)
            times = {op: [] for op in OPERATIONS}
            written_bytes = 0
            written_time_ms = 0.0
            read_bytes = 0
            read_time_ms = 0.0

            for i in range(iterations):
                file_path = f"{mount_path}{size_name}_file_{i}.bin"

                latency = timed(client.write, file_path, payload)
                times["write"].append(latency)
                written_bytes += data_size
                written_time_ms += latency

                start = time.perf_counter()
                read_result = client.read(file_path)
                latency = (time.perf_counter() - start) * 1000.0
                times["read"].append(latency)
                read_bytes += len(read_result) if isinstance(
                    read_result, (bytes, bytearray, str)
                ) else data_size
                read_time_ms += latency

                times["stat"].append(timed(client.stat, file_path))
                times["ls"].append(timed(client.ls, mount_path))
                times["rm"].append(timed(client.rm, file_path))

            dir_path = f"{mount_path}{size_name}_dir/"
            # AGFS mkdir follows POSIX semantics: the parent must already
            # exist (memfs returns "not found" for the missing parent), so
            # create it outside the timed loop and clean up afterwards.
            client.mkdir(dir_path, "755")
            for i in range(iterations):
                test_dir = f"{dir_path}subdir_{i}/"
                times["mkdir"].append(timed(client.mkdir, test_dir, "755"))
                client.rm(test_dir, recursive=True)
            client.rm(dir_path, recursive=True)

            grep_payload = (b"hello world benchmark test openviking context "
                            * (data_size // 40 + 1))[:data_size]
            grep_path = f"{mount_path}{size_name}_grep_data.bin"
            client.write(grep_path, grep_payload)
            for _ in range(iterations):
                times["grep"].append(timed(
                    client.grep, grep_path, "benchmark", False, True))
            client.rm(grep_path)

            size_results = {}
            for op in OPERATIONS:
                avg_ms, ops_sec = stats_ms(times[op])
                size_results[f"{op}_avg_latency_ms"] = avg_ms
                size_results[f"{op}_ops_per_sec"] = ops_sec
            if written_time_ms > 0:
                size_results["write_throughput_mbs"] = round(
                    written_bytes / (written_time_ms / 1000.0) / (1024 * 1024), 2)
            else:
                size_results["write_throughput_mbs"] = 0.0
            if read_time_ms > 0:
                size_results["read_throughput_mbs"] = round(
                    read_bytes / (read_time_ms / 1000.0) / (1024 * 1024), 2)
            else:
                size_results["read_throughput_mbs"] = 0.0
            size_results["data_size_bytes"] = data_size
            results_summary[size_name] = size_results

            for op in OPERATIONS:
                print(f"[context_fs] {size_name} {op}: "
                      f"avg_latency_ms={size_results[f'{op}_avg_latency_ms']}, "
                      f"ops_per_sec={size_results[f'{op}_ops_per_sec']}", flush=True)
            print(f"[context_fs] {size_name} write: "
                  f"throughput_mbs={size_results['write_throughput_mbs']}, "
                  f"read: throughput_mbs={size_results['read_throughput_mbs']}",
                  flush=True)

        output = {
            "benchmark": "context_fs",
            "description": "OpenViking AGFS filesystem operations benchmark",
            "reference": "https://github.com/volcengine/OpenViking",
            "software": "openviking",
            "version": software_version,
            "architecture": architecture,
            "performance_metrics": {
                "ops_per_sec": {"unit": "ops/s",
                                "description": "Filesystem operations per second"},
                "avg_latency_ms": {"unit": "ms",
                                   "description": "Average operation latency"},
                "throughput_mbs": {"unit": "MB/s",
                                   "description": "Data throughput"},
            },
            "parameters": {
                "iterations": iterations,
                "data_sizes_bytes": [size for _, size in SIZE_CONFIGS],
                "operations": list(OPERATIONS),
            },
            "results_summary": results_summary,
        }
        with open(output_file, "w", encoding="utf-8") as handle:
            json.dump(output, handle, indent=2)
        print(f"[context_fs] output written to {output_file}")
    finally:
        try:
            client.rm(mount_path, recursive=True)
        except Exception:
            pass
        try:
            client.unmount(mount_path)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        raise SystemExit("iterations must be a positive integer")
    run_context_benchmark(args.results, args.iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
