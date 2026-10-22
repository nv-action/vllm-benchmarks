#!/usr/bin/env python3
"""Run the original sra_test hnswlib_test on five ANN-Benchmarks datasets."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import subprocess
import sys
from pathlib import Path

DATASETS = (
    "sift-128-euclidean",
    "glove-100-angular",
    "deep-image-96-angular",
    "fashion-mnist-784-euclidean",
    "gist-960-euclidean",
)
REVISION = "9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf"
FIELDS = {
    "build_time_s": (r"^\s*build\+prepare_time\s*=\s*([0-9.eE+-]+)\s+s\s*$", "s", "lower_is_better"),
    "recall": (r"^\s*recall\s*=\s*([0-9.eE+-]+)\s*$", "ratio", "higher_is_better"),
    "qps_wall": (r"^\s*final_QPS\(mid50 wall mean\)\s*=\s*([0-9.eE+-]+)\s*$", "queries/s", "higher_is_better"),
    "qps_avg_thread": (
        r"^\s*QPS_from_avg_thread_time\s*\*\s*threads\s*=\s*([0-9.eE+-]+)\s*$",
        "queries/s",
        "neutral",
    ),
}


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def prepare_dataset(directory: Path, name: str) -> dict[str, object]:
    path = directory / f"{name}.hdf5"
    if not path.is_file():
        directory.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".hdf5.download")
        url = f"https://ann-benchmarks.com/{path.name}"
        print(f"[hnswlib-sra] downloading {url}", flush=True)
        try:
            subprocess.run(
                ["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30", "--output", str(temporary), url],
                check=True,
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        print(f"[hnswlib-sra] using local dataset {path}", flush=True)
    if path.stat().st_size == 0:
        raise RuntimeError(f"dataset is empty: {path}")
    for field in ("train", "test", "neighbors"):
        completed = subprocess.run(
            ["h5dump", "-H", "-d", f"/{field}", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode:
            raise RuntimeError(f"dataset {path} is missing or has invalid {field}: {completed.stderr.strip()}")
    return {"sha256": checksum(path), "bytes": path.stat().st_size}


def read_config(path: Path) -> dict[str, str]:
    if not path.is_file():
        raise RuntimeError(f"sra_test configuration is missing: {path}")
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, separator, value = line.partition("=")
        if not separator:
            raise RuntimeError(f"invalid sra_test configuration line in {path}: {raw}")
        values[key.strip()] = value.strip()
    if values.get("save_or_load") != "save" or values.get("batch_mode") != "false":
        raise RuntimeError(f"sra_test must build a fresh index in single-query mode: {path}")
    return values


def parse_summary(output: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for field, (pattern, _, _) in FIELDS.items():
        matches = re.findall(pattern, output, re.MULTILINE)
        if len(matches) != 1:
            raise RuntimeError(f"sra_test output must contain exactly one {field}, found {len(matches)}")
        value = float(matches[0])
        if not math.isfinite(value) or value < 0 or (field.startswith("qps") and value == 0):
            raise RuntimeError(f"sra_test {field} is invalid: {value}")
        values[field] = value
    if values["recall"] > 1:
        raise RuntimeError(f"sra_test recall exceeds 1: {values['recall']}")
    return values


def run_case(source: Path, results: Path, dataset: str) -> dict[str, float]:
    command = [str(source / "hnswlib_test"), "hnswlib", dataset, "--no-pin"]
    logfile = results / "sra-test-logs" / f"hnswlib_{dataset}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    print(f"[hnswlib-sra] {' '.join(command)}", flush=True)
    with logfile.open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            command, cwd=source, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            log.write(line)
            print(line, end="", flush=True)
        returncode = process.wait()
    if returncode:
        raise RuntimeError(f"sra_test hnswlib/{dataset} exited with code {returncode}; see {logfile}")
    return parse_summary(logfile.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True)
    args = parser.parse_args()
    args.results.mkdir(parents=True, exist_ok=True)
    data_link = args.source / "data"
    if data_link.exists() or data_link.is_symlink():
        raise RuntimeError(f"sra_test data path must be clean: {data_link}")
    data_link.symlink_to(args.data.resolve(), target_is_directory=True)

    results: dict[str, dict[str, object]] = {}
    datasets: dict[str, dict[str, object]] = {}
    configurations: dict[str, dict[str, str]] = {}
    for dataset in DATASETS:
        datasets[dataset] = prepare_dataset(args.data, dataset)
        config = args.source / "configs" / "hnswlib" / f"hnswlib_{dataset}.config"
        values = read_config(config)
        configurations[dataset] = {key: value for key, value in values.items() if key != "index_path"}
        summary = run_case(args.source, args.results, dataset)
        for field, value in summary.items():
            _, unit, direction = FIELDS[field]
            name = f"hnswlib/{dataset}/{field}"
            results[name] = {
                "source_name": name,
                "group": dataset,
                "value": value,
                "unit": unit,
                "direction": direction,
            }
    payload = {
        "benchmark": "sra_test_hnswlib_same_nq",
        "software": "hnswlib",
        "version": args.version,
        "architecture": args.architecture,
        "parameters": {
            "sra_test_revision": REVISION,
            "algorithm": "hnswlib",
            "datasets": datasets,
            "configurations": configurations,
            "pin_threads": False,
        },
        "results": results,
    }
    (args.results / "benchmark_sra.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(f"[hnswlib-sra] normalized {len(results)} official measurements", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[hnswlib-sra] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
