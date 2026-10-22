#!/usr/bin/env python3
"""Run the OpenViking micro benchmark.

Ports the upstream openEuler reference scheme: import/reload latency, AGFS
binding client initialization, per-operation filesystem latencies across
1KB/64KB/1MB payloads, and multithreaded write scaling on an in-memory
"memfs" filesystem.
"""

from __future__ import annotations

import argparse
import importlib
import json
import platform
import threading
import time


SIZE_CONFIGS = (
    ("1kb", 1024),
    ("64kb", 65536),
    ("1mb", 1048576),
)
MICRO_OPERATIONS = ("write", "read", "stat", "ls", "rm")
THREAD_COUNTS = (1, 2, 4, 8, 32)
MULTITHREAD_TOTAL_OPS = 50


def get_binding_client():
    from openviking.pyagfs import get_binding_client as _get_binding_client

    return _get_binding_client()


def stats_ms(latencies):
    if not latencies:
        return 0.0, 0.0
    total = sum(latencies)
    avg = total / len(latencies)
    ops = len(latencies) * 1000.0 / total if total > 0 else 0.0
    return round(avg, 3), round(ops, 2)


def run_micro_benchmark(output_file, iterations):
    architecture = platform.machine()
    print(f"[micro] openviking arch={architecture} iterations={iterations}")
    results = {}

    # The first import of openviking in this process is the cold import; its
    # duration is the import_init metric from the reference scheme.
    start = time.perf_counter()
    import openviking
    import_time_ms = round((time.perf_counter() - start) * 1000.0, 3)
    start = time.perf_counter()
    importlib.reload(openviking)
    reload_time_ms = round((time.perf_counter() - start) * 1000.0, 3)
    software_version = openviking.__version__
    results["import_init"] = {
        "import_time_ms": import_time_ms,
        "reload_time_ms": reload_time_ms,
        "version": software_version,
    }
    print(f"[micro] import_init: import_time_ms={import_time_ms}, "
          f"reload_time_ms={reload_time_ms}", flush=True)

    BindingClient, _FileHandle = get_binding_client()
    mount_path = "viking://micro-bench/"
    start = time.perf_counter()
    client = BindingClient()
    client_init_ms = round((time.perf_counter() - start) * 1000.0, 3)
    start = time.perf_counter()
    client.health()
    health_check_ms = round((time.perf_counter() - start) * 1000.0, 3)
    start = time.perf_counter()
    client.get_capabilities()
    get_capabilities_ms = round((time.perf_counter() - start) * 1000.0, 3)
    results["client_init"] = {
        "client_init_ms": client_init_ms,
        "health_check_ms": health_check_ms,
        "get_capabilities_ms": get_capabilities_ms,
    }
    print(f"[micro] client_init: client_init_ms={client_init_ms}, "
          f"health_check_ms={health_check_ms}, "
          f"get_capabilities_ms={get_capabilities_ms}", flush=True)

    client.mount("memfs", mount_path)
    try:
        fs_ops = {}
        for size_name, data_size in SIZE_CONFIGS:
            payload = b"\x00" * data_size
            op_times = {}
            for op_name in MICRO_OPERATIONS:
                latencies = []
                for i in range(iterations):
                    file_path = f"{mount_path}{size_name}_micro_{i}.bin"
                    start = time.perf_counter()
                    if op_name == "write":
                        client.write(file_path, payload)
                    elif op_name == "read":
                        client.read(file_path)
                    elif op_name == "stat":
                        client.stat(file_path)
                    elif op_name == "ls":
                        client.ls(mount_path)
                    elif op_name == "rm":
                        try:
                            client.rm(file_path)
                        except Exception:
                            pass
                    latencies.append((time.perf_counter() - start) * 1000.0)
                avg_ms, ops_sec = stats_ms(latencies)
                op_times[f"{op_name}_avg_latency_ms"] = avg_ms
                op_times[f"{op_name}_ops_per_sec"] = ops_sec
                if op_name in ("write", "read") and avg_ms > 0:
                    op_times[f"{op_name}_throughput_mbs"] = round(
                        data_size / (avg_ms / 1000.0) / (1024 * 1024), 2)
            fs_ops[size_name] = op_times
            for op_name in MICRO_OPERATIONS:
                print(f"[micro] fs_ops {size_name} {op_name}: "
                      f"avg_latency_ms={op_times[f'{op_name}_avg_latency_ms']}, "
                      f"ops_per_sec={op_times[f'{op_name}_ops_per_sec']}",
                      flush=True)
            # Clean the files written while timing the "write" operation.
            for i in range(iterations):
                try:
                    client.rm(f"{mount_path}{size_name}_micro_{i}.bin")
                except Exception:
                    pass
        results["fs_ops_latency"] = fs_ops

        mt_results = {}
        mt_payload = b"\x00" * 65536
        for thread_count in THREAD_COUNTS:
            per_thread_ops = max(1, MULTITHREAD_TOTAL_OPS // thread_count)
            thread_results = []
            lock = threading.Lock()

            def run_and_capture(thread_id, count):
                local_ops = 0
                local_time_ms = 0.0
                for j in range(count):
                    path = f"{mount_path}mt_{thread_count}_{thread_id}_{j}.bin"
                    start = time.perf_counter()
                    try:
                        client.write(path, mt_payload)
                        local_ops += 1
                    except Exception:
                        pass
                    local_time_ms += (time.perf_counter() - start) * 1000.0
                    try:
                        client.rm(path)
                    except Exception:
                        pass
                with lock:
                    thread_results.append((local_ops, local_time_ms))

            threads = []
            start = time.perf_counter()
            for thread_id in range(thread_count):
                thread = threading.Thread(
                    target=run_and_capture, args=(thread_id, per_thread_ops))
                threads.append(thread)
                thread.start()
            for thread in threads:
                thread.join()
            wall_time_ms = round((time.perf_counter() - start) * 1000.0, 3)

            ops_completed = sum(r[0] for r in thread_results)
            total_time_ms = sum(r[1] for r in thread_results)
            combined_ops_sec = (round(ops_completed * 1000.0 / wall_time_ms, 2)
                                if wall_time_ms > 0 else 0.0)
            avg_per_op_ms = (round(total_time_ms / ops_completed, 3)
                             if ops_completed > 0 else 0.0)
            mt_results[f"threads_{thread_count}"] = {
                "total_ops": ops_completed,
                "wall_time_ms": wall_time_ms,
                "combined_ops_per_sec": combined_ops_sec,
                "avg_per_op_latency_ms": avg_per_op_ms,
            }
            print(f"[micro] multithread threads_{thread_count}: "
                  f"combined_ops_per_sec={combined_ops_sec}, "
                  f"avg_per_op_latency_ms={avg_per_op_ms}", flush=True)
        results["multithread_fs_ops"] = mt_results
    finally:
        try:
            client.rm(mount_path, recursive=True)
        except Exception:
            pass
        try:
            client.unmount(mount_path)
        except Exception:
            pass

    output = {
        "benchmark": "micro_operations",
        "description": "OpenViking micro benchmarks: import/init latency, "
                       "filesystem ops, multithread scaling",
        "reference": "https://github.com/volcengine/OpenViking",
        "software": "openviking",
        "version": software_version,
        "architecture": architecture,
        "performance_metrics": {
            "ops_per_sec": {"unit": "ops/s",
                            "description": "Operations per second"},
            "avg_latency_ms": {"unit": "ms",
                               "description": "Average latency in milliseconds"},
            "init_time_ms": {"unit": "ms",
                             "description": "Initialization time"},
        },
        "parameters": {
            "iterations": iterations,
            "data_sizes_bytes": [size for _, size in SIZE_CONFIGS],
            "thread_counts": list(THREAD_COUNTS),
        },
        "results": results,
    }
    with open(output_file, "w", encoding="utf-8") as handle:
        json.dump(output, handle, indent=2)
    print(f"[micro] output written to {output_file}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--results", required=True)
    parser.add_argument("--iterations", type=int, default=3)
    args = parser.parse_args()
    if args.iterations < 1:
        raise SystemExit("iterations must be a positive integer")
    run_micro_benchmark(args.results, args.iterations)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
