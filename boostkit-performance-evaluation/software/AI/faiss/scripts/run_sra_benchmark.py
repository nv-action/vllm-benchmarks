#!/usr/bin/env python3
"""Run the pinned sra_test Faiss binaries and preserve their official output."""

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
ALGORITHMS = ("hnsw", "ivfpq", "ivfpqfs", "pqfs", "ivfflat", "ivfrabitq", "ivfrabitqfs")
REVISION = "9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf"
FIELD_PATTERNS = {
    "build_time_s": re.compile(r"^\s*build\+prepare_time\s*=\s*([0-9.eE+-]+)\s+s\s*$", re.MULTILINE),
    "recall": re.compile(r"^\s*recall\s*=\s*([0-9.eE+-]+)\s*$", re.MULTILINE),
    "qps_wall": re.compile(r"^\s*final_QPS\(mid50 wall mean\)\s*=\s*([0-9.eE+-]+)\s*$", re.MULTILINE),
    "qps_avg_thread": re.compile(r"^\s*QPS_from_avg_thread_time\s*\*\s*threads\s*=\s*([0-9.eE+-]+)\s*$", re.MULTILINE),
}
METRIC_FIELDS = {
    "build_time_s": ("s", "lower_is_better"),
    "recall": ("ratio", "higher_is_better"),
    "qps_wall": ("queries/s", "higher_is_better"),
    "qps_avg_thread": ("queries/s", "neutral"),
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
        print(f"[faiss-sra] downloading {url}", flush=True)
        try:
            subprocess.run(
                ["curl", "--fail", "--location", "--retry", "3", "--connect-timeout", "30", "--output", str(temporary), url],
                check=True,
            )
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        print(f"[faiss-sra] using local dataset {path}", flush=True)
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
    if values.get("save_or_load") != "save":
        raise RuntimeError(f"sra_test configuration must build and save a fresh index: {path}")
    if values.get("batch_mode") != "false":
        raise RuntimeError(f"sra_test configuration must use single-query mode: {path}")
    return values


def parse_summary(output: str) -> dict[str, float]:
    values: dict[str, float] = {}
    for field, pattern in FIELD_PATTERNS.items():
        matches = pattern.findall(output)
        if len(matches) != 1:
            raise RuntimeError(f"sra_test output must contain exactly one {field}, found {len(matches)}")
        value = float(matches[0])
        if not math.isfinite(value) or value < 0 or (field.startswith("qps") and value == 0):
            raise RuntimeError(f"sra_test {field} is invalid: {value}")
        values[field] = value
    if values["recall"] > 1:
        raise RuntimeError(f"sra_test recall exceeds 1: {values['recall']}")
    return values


def run_case(source: Path, result_dir: Path, algorithm: str, dataset: str) -> dict[str, float]:
    command = [str(source / f"{algorithm}_test"), algorithm, dataset, "--no-pin"]
    logfile = result_dir / "sra-test-logs" / f"{algorithm}_{dataset}.log"
    logfile.parent.mkdir(parents=True, exist_ok=True)
    print(f"[faiss-sra] {' '.join(command)}", flush=True)
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
        raise RuntimeError(f"sra_test {algorithm}/{dataset} exited with code {returncode}; see {logfile}")
    return parse_summary(logfile.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--data", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True)
    parser.add_argument("--algorithms", nargs="+", choices=ALGORITHMS, default=["hnsw"])
    args = parser.parse_args()
    if args.version != "1.14.3":
        raise RuntimeError("sra_test Faiss evaluation is verified only for Faiss 1.14.3")
    if len(set(args.algorithms)) != len(args.algorithms):
        raise RuntimeError("sra_test algorithm list contains duplicates")
    args.results.mkdir(parents=True, exist_ok=True)
    data_link = args.source / "data"
    if data_link.exists() or data_link.is_symlink():
        raise RuntimeError(f"sra_test data path must be clean: {data_link}")
    data_link.symlink_to(args.data.resolve(), target_is_directory=True)

    results: dict[str, dict[str, object]] = {}
    dataset_info: dict[str, dict[str, object]] = {}
    configurations: dict[str, dict[str, str]] = {}
    for algorithm in args.algorithms:
        for dataset in DATASETS:
            if dataset not in dataset_info:
                dataset_info[dataset] = prepare_dataset(args.data, dataset)
            scenario = f"{algorithm}/{dataset}"
            config = args.source / "configs" / algorithm / f"{algorithm}_{dataset}.config"
            configuration = read_config(config)
            configurations[scenario] = {key: value for key, value in configuration.items() if key != "index_path"}
            summary = run_case(args.source, args.results, algorithm, dataset)
            for field, value in summary.items():
                unit, direction = METRIC_FIELDS[field]
                name = f"{scenario}/{field}"
                results[name] = {
                    "source_name": name,
                    "group": dataset,
                    "value": value,
                    "unit": unit,
                    "direction": direction,
                }
    payload = {
        "benchmark": "sra_test_faiss_same_nq",
        "software": "faiss",
        "version": args.version,
        "architecture": args.architecture,
        "parameters": {
            "sra_test_revision": REVISION,
            "algorithms": args.algorithms,
            "datasets": dataset_info,
            "configurations": configurations,
            "pin_threads": False,
        },
        "results": results,
    }
    (args.results / "benchmark_sra.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8"
    )
    print(f"[faiss-sra] normalized {len(results)} official measurements", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"[faiss-sra] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1) from error
