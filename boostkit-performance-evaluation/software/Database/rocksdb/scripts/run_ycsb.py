#!/usr/bin/env python3
"""Run the document's local YCSB load/warmup/A/C flow without tuning.

The original YCSB launcher, workloads, and RocksDB binding are not edited.
Every operation uses the same task-private RocksDB directory on persistent disk.
"""

from __future__ import annotations

import json
import hashlib
import math
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


RECORDS = 100_000_000
OPERATIONS = 100_000_000
FIELD_LENGTH = 256
FIELD_COUNT = 1
RUN_THREADS = 5
WARMUP_THREADS = 3
WORKLOADS = ("workloada", "workloadc")
YCSB_REVISION = "19e885f7cb780fdded0547853f7810a150554caf"
THROUGHPUT = re.compile(r"^\[OVERALL\],\s*Throughput\(ops/sec\),\s*([0-9]+(?:\.[0-9]+)?)\s*$")
ERROR_COUNT = re.compile(r"^\[[^]]+\],\s*Return=ERROR,\s*([0-9]+)\s*$")


def run_phase(distribution: Path, action: str, workload: str, threads: int,
              data_dir: Path, raw_log: object, phase: str) -> float:
    command = [
        sys.executable, str(distribution / "bin" / "ycsb"), action, "rocksdb", "-s",
        "-P", str(distribution / "workloads" / workload),
        "-p", f"rocksdb.dir={data_dir}",
        "-p", f"recordcount={RECORDS}",
        "-p", f"operationcount={OPERATIONS}",
        "-p", f"fieldlength={FIELD_LENGTH}",
        "-p", f"fieldcount={FIELD_COUNT}",
        "-p", "rocksdb.cfs=default", "-p", "table=default",
        "-threads", str(threads),
    ]
    print(f"[rocksdb-ycsb] {phase}: {shlex.join(command)}", flush=True)
    raw_log.write(f"\n=== {phase} ===\n$ {shlex.join(command)}\n")
    values: list[float] = []
    errors = 0
    with subprocess.Popen(command, cwd=distribution, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                          errors="replace", bufsize=1) as process:
        assert process.stdout is not None
        for line in process.stdout:
            raw_log.write(line)
            raw_log.flush()
            print(line, end="", flush=True)
            if match := THROUGHPUT.fullmatch(line.strip()):
                values.append(float(match.group(1)))
            if match := ERROR_COUNT.fullmatch(line.strip()):
                errors += int(match.group(1))
        return_code = process.wait()
    if return_code:
        raise RuntimeError(f"{phase} exited with code {return_code}")
    if len(values) != 1 or not math.isfinite(values[0]) or values[0] <= 0:
        raise RuntimeError(f"{phase} must produce exactly one positive YCSB throughput")
    if errors:
        raise RuntimeError(f"{phase} reported {errors} failed YCSB operations")
    return values[0]


def main() -> int:
    if len(sys.argv) != 4:
        print("usage: run_ycsb.py YCSB_DISTRIBUTION RESULTS_JSON RAW_LOG", file=sys.stderr)
        return 2
    distribution, result_path, raw_path = map(Path, sys.argv[1:])
    work_dir = Path(os.environ["PERF_WORK_DIR"]).resolve()
    data_root = Path(os.environ["ROCKSDB_BENCH_DATA_DIR"]).resolve()
    if data_root == work_dir or work_dir not in data_root.parents:
        raise RuntimeError("RocksDB data directory must be a task-private child of PERF_WORK_DIR")
    if not (distribution / "bin" / "ycsb").is_file():
        raise RuntimeError("the original YCSB launcher is unavailable")
    jni_jar = distribution / "lib" / f"rocksdbjni-{os.environ['SOFTWARE_VERSION']}.jar"
    if not jni_jar.is_file():
        raise RuntimeError(f"the locally built RocksDB JNI JAR is unavailable: {jni_jar}")
    java_version = subprocess.run(
        ["java", "-version"], capture_output=True, text=True, check=True
    )
    version_lines = (java_version.stderr or java_version.stdout).splitlines()
    if not version_lines:
        raise RuntimeError("Java did not report its version")
    jni_checksum = hashlib.sha256(jni_jar.read_bytes()).hexdigest()
    db_dir = data_root / "workload_data"
    snapshot = data_root / "warmup_workload_data"
    if any(path.exists() for path in (db_dir, snapshot)):
        raise RuntimeError("YCSB data or snapshot directory is not empty")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    raw_path.parent.mkdir(parents=True, exist_ok=True)

    with raw_path.open("w", encoding="utf-8", buffering=1) as raw_log:
        # Same ordering as the document's physical-machine single-instance flow.
        run_phase(distribution, "load", "workloada", RUN_THREADS, db_dir, raw_log, "load")
        run_phase(distribution, "run", "workloadc", WARMUP_THREADS, db_dir, raw_log,
                  "warmup before snapshot")
        print("[rocksdb-ycsb] copying the closed, warmed DB to a private snapshot", flush=True)
        shutil.copytree(db_dir, snapshot)
        shutil.rmtree(db_dir)
        shutil.copytree(snapshot, db_dir)
        run_phase(distribution, "run", "workloadc", WARMUP_THREADS, db_dir, raw_log,
                  "warmup after restore")

        results: dict[str, dict[str, object]] = {}
        for workload in WORKLOADS:
            throughput = run_phase(distribution, "run", workload, RUN_THREADS, db_dir,
                                   raw_log, f"formal {workload}")
            name = f"{workload} throughput"
            results[name] = {
                "source_name": name, "source_field": "[OVERALL], Throughput(ops/sec)",
                "group": workload, "value": throughput, "unit": "ops/sec",
                "direction": "higher_is_better",
            }

    payload = {
        "benchmark": "ycsb_rocksdb_physical_single_instance",
        "software": "rocksdb", "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "ycsb_revision": YCSB_REVISION, "workloads": list(WORKLOADS),
            "recordcount": RECORDS, "operationcount": OPERATIONS,
            "fieldlength": FIELD_LENGTH, "fieldcount": FIELD_COUNT,
            "load_threads": RUN_THREADS, "warmup_threads": WARMUP_THREADS,
            "run_threads": RUN_THREADS, "rocksdb_optionsfile": None,
            "cpu_affinity": None, "warmup_runs": 2,
        },
        "runtime_context": {
            "java_version": version_lines[0],
            "jni_jar_sha256": jni_checksum,
        },
        "results": results,
    }
    result_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[rocksdb-ycsb] recorded {len(results)} workload throughput metrics", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
