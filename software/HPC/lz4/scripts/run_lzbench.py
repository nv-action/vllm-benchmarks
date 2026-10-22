#!/usr/bin/env python3
"""Run the lzbench LZ4 case and normalize its three measurements."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


RESULT_PATTERN = re.compile(
    r"^lz4\s+1\.9\.4\s+([0-9.]+)\s+MB/s\s+([0-9.]+)\s+MB/s\s+"
    r"([0-9]+)\s+([0-9.]+)\s+(.+)$"
)
PARAMS_PATTERN = re.compile(
    r"\[Params\].*\bcTime=20(?:\.0)?\b.*\bdTime=20(?:\.0)?\b.*\bchunkSize=4KB\b"
)
SILESIA_SHA256 = "ea122ed051dc7a6c58d2bb56bb05b34d9f1537c4dc9e71519142e2ca8cd6338d"


def parse_lzbench(output: str, corpus: Path) -> dict[str, dict[str, object]]:
    rows = [
        match for line in output.splitlines()
        if (match := RESULT_PATTERN.match(line.strip()))
    ]
    if len(rows) != 1:
        raise RuntimeError(f"expected exactly one lz4 1.9.4 result, found {len(rows)}")
    if not any(PARAMS_PATTERN.search(line) for line in output.splitlines()):
        raise RuntimeError("lzbench did not confirm the -b4 -t20u20 parameters")

    compression, decompression, compressed_size, ratio, filename = rows[0].groups()
    if Path(filename).resolve() != corpus.resolve():
        raise RuntimeError(f"lzbench measured an unexpected file: {filename}")
    values = [float(compression), float(decompression), float(ratio)]
    if any(not math.isfinite(value) or value <= 0 for value in values):
        raise RuntimeError("lzbench returned a non-positive or non-finite measurement")
    compressed_bytes = int(compressed_size)
    expected_ratio = compressed_bytes / corpus.stat().st_size * 100
    if abs(values[2] - expected_ratio) > 0.02:
        raise RuntimeError("lzbench ratio does not match its compressed byte count")

    return {
        "compression_throughput": {
            "source_metric": "Compress.", "value": values[0], "unit": "MB/s",
            "direction": "higher_is_better",
        },
        "decompression_throughput": {
            "source_metric": "Decompress.", "value": values[1], "unit": "MB/s",
            "direction": "higher_is_better",
        },
        "compressed_size_ratio": {
            "source_metric": "Ratio", "value": values[2], "unit": "%",
            "direction": "lower_is_better",
        },
    }


def main() -> int:
    if len(sys.argv) != 5:
        print("usage: run_lzbench.py LZBENCH SILESIA_TAR RAW_OUTPUT NORMALIZED_OUTPUT", file=sys.stderr)
        return 1
    binary, corpus, raw_output, normalized_output = map(Path, sys.argv[1:])
    if not binary.is_file() or not os.access(binary, os.X_OK):
        raise RuntimeError(f"lzbench executable is unavailable: {binary}")
    if not corpus.is_file():
        raise RuntimeError(f"Silesia corpus is unavailable: {corpus}")

    command = [str(binary.resolve()), "-elz4", "-b4", "-t20u20", str(corpus.resolve())]
    print("[lz4-benchmark] " + " ".join(command), flush=True)
    completed = subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", timeout=1800, check=False,
    )
    print(completed.stdout, end="", flush=True)
    raw_output.parent.mkdir(parents=True, exist_ok=True)
    raw_output.write_text(completed.stdout, encoding="utf-8")
    if completed.returncode:
        raise RuntimeError(f"lzbench exited with code {completed.returncode}")
    results = parse_lzbench(completed.stdout, corpus)

    digest = hashlib.sha256()
    with corpus.open("rb") as corpus_stream:
        for block in iter(lambda: corpus_stream.read(1024 * 1024), b""):
            digest.update(block)
    actual_sha256 = digest.hexdigest()
    if actual_sha256 != SILESIA_SHA256:
        raise RuntimeError("Silesia corpus checksum changed during the test")
    normalized = {
        "benchmark": "lz4_lzbench", "software": "lz4",
        "version": os.environ["SOFTWARE_VERSION"],
        "architecture": os.environ["EXPECTED_ARCH"],
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "parameters": {
            "command": ["lzbench", *command[1:-1], "silesia.tar"],
            "lzbench_version": "2.2", "lz4_version": "1.9.4",
            "block_size_kib": 4, "compression_seconds": 20,
            "decompression_seconds": 20, "dataset_sha256": actual_sha256,
        },
        "runtime_context": {"command": command, "input_size_bytes": corpus.stat().st_size},
        "results": results,
    }
    normalized_output.parent.mkdir(parents=True, exist_ok=True)
    normalized_output.write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"[lz4-benchmark] normalized {len(results)} lzbench metrics", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
