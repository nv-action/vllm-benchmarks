#!/usr/bin/env python3
"""Benchmark ScaNN vector similarity search with a deterministic synthetic dataset.

The benchmark builds an index and runs query-only search for two officially
supported distance measures over the same dataset:

* ``dot_product`` --- maximum inner product search on unit-normalized vectors,
  equivalent to cosine similarity.
* ``squared_l2`` --- squared Euclidean distance search.

For every distance measure it records independent build-and-search samples, then
reports their medians for index build time, queries per second, per-query
latency, and recall@K against an exact brute-force ground truth.
"""

from __future__ import annotations

import json
import os
import statistics
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import scann

OUTPUT_FILE = Path(os.environ["RESULTS_DIR"]) / "benchmark.json"

SCALE_TO_VECTOR_COUNT = {
    "1K": 1_000,
    "10K": 10_000,
    "100K": 100_000,
    "1M": 1_000_000,
}

_DISTANCE_MEASURES = ("dot_product", "squared_l2")


def environment_integer(name: str, default: int, minimum: int) -> int:
    raw_value = os.environ.get(name, "")
    if not raw_value:
        return default
    try:
        value = int(raw_value)
    except ValueError:
        raise SystemExit(f"invalid integer for {name}: {raw_value}")
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}")
    return value


def environment_float(name: str, default: float, minimum: float) -> float:
    raw_value = os.environ.get(name, "")
    if not raw_value:
        return default
    try:
        value = float(raw_value)
    except ValueError:
        raise SystemExit(f"invalid float for {name}: {raw_value}")
    if value < minimum:
        raise SystemExit(f"{name} must be at least {minimum}")
    return value


def environment_choice(name: str, default: str, choices: tuple[str, ...]) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    if value not in choices:
        raise SystemExit(f"{name} must be one of {choices}")
    return value


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def normalize_unit(vectors: np.ndarray) -> np.ndarray:
    normalized = vectors.copy()
    norms = np.linalg.norm(normalized, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    norms = norms.astype(normalized.dtype, copy=False)
    return normalized / norms


def exact_neighbors(
    database: np.ndarray, queries: np.ndarray, k: int, distance: str
) -> np.ndarray:
    """Return exact top-k neighbor indices (num_queries, k) via brute force."""
    if distance == "dot_product":
        scores = queries @ database.T
        return np.argpartition(-scores, k - 1, axis=1)[:, :k]
    distances = (
        np.sum(database * database, axis=1)[None, :]
        + np.sum(queries * queries, axis=1)[:, None]
        - 2.0 * (queries @ database.T)
    )
    return np.argpartition(distances, k - 1, axis=1)[:, :k]


def recall_at_k(neighbors: np.ndarray, ground_truth: np.ndarray, k: int) -> float:
    matches = 0
    for approximate, exact in zip(neighbors, ground_truth):
        matches += len(set(approximate.tolist()) & set(exact.tolist()))
    return matches / (ground_truth.shape[0] * k)


def build_searcher(
    database: np.ndarray,
    distance: str,
    k: int,
    num_leaves: int,
    dims_per_block: int,
    anisotropic_quantization_threshold: float,
    reorder: int,
) -> Any:
    spherical = distance == "dot_product"
    builder = scann.scann_ops_pybind.builder(database, k, distance)
    builder = builder.tree(
        num_leaves=num_leaves,
        num_leaves_to_search=1,
        training_sample_size=min(database.shape[0], 100_000),
        spherical=spherical,
        quantize_centroids=True,
    )
    builder = builder.score_ah(
        dimensions_per_block=dims_per_block,
        anisotropic_quantization_threshold=anisotropic_quantization_threshold,
    )
    builder = builder.reorder(reorder)
    return builder.build()


def measure_search(
    searcher: Any,
    queries: np.ndarray,
    ground_truth: np.ndarray,
    k: int,
    num_leaves: int,
    reorder: int,
) -> dict[str, Any]:
    neighbors_all: list[np.ndarray] = []
    search_start = time.perf_counter()
    for query in queries:
        neighbors, _ = searcher.search(
            query,
            final_num_neighbors=k,
            pre_reorder_num_neighbors=reorder,
            leaves_to_search=num_leaves,
        )
        neighbors_all.append(neighbors)
    search_seconds = time.perf_counter() - search_start

    neighbors = np.asarray(neighbors_all, dtype=np.int32)
    total_queries = neighbors.shape[0]
    qps = total_queries / search_seconds if search_seconds > 0 else 0.0
    latency_us = (
        (search_seconds * 1_000_000.0) / total_queries if total_queries > 0 else 0.0
    )
    recall = recall_at_k(neighbors, ground_truth, k)

    return {
        "search_time_s": float(search_seconds),
        "qps": float(qps),
        "latency_per_query_us": float(latency_us),
        "recall_at_k": float(recall),
    }


def main() -> None:
    data_scale = environment_choice("DATA_SCALE", "100K", tuple(SCALE_TO_VECTOR_COUNT))
    num_vectors = SCALE_TO_VECTOR_COUNT[data_scale]
    dimension = environment_integer("DATA_DIM", 128, 2)
    iterations = environment_integer("ITERATIONS", 3, 1)
    k = environment_integer("K", 10, 1)
    seed = environment_integer("SCANN_BENCHMARK_SEED", 42, 0)
    num_queries = min(1000, num_vectors // 10)
    num_leaves = environment_integer("SCANN_NUM_LEAVES", 200, 1)
    dims_per_block = environment_integer("SCANN_DIMS_PER_BLOCK", 2, 1)
    anisotropic_quantization_threshold = environment_float(
        "SCANN_ANISOTROPIC_QUANTIZATION_THRESHOLD", 0.2, 0.0
    )
    reorder = environment_integer("SCANN_REORDER", 100, 1)

    rng = np.random.default_rng(seed)
    base_vectors = rng.random((num_vectors, dimension), dtype=np.float32)
    base_queries = rng.random((num_queries, dimension), dtype=np.float32)

    results: dict[str, Any] = {}
    for distance in _DISTANCE_MEASURES:
        database = (
            normalize_unit(base_vectors) if distance == "dot_product" else base_vectors
        )
        queries = (
            normalize_unit(base_queries) if distance == "dot_product" else base_queries
        )

        ground_truth = exact_neighbors(database, queries, k, distance)

        samples: list[dict[str, Any]] = []
        for iteration in range(iterations):
            build_start = time.perf_counter()
            searcher = build_searcher(
                database,
                distance,
                k,
                num_leaves,
                dims_per_block,
                anisotropic_quantization_threshold,
                reorder,
            )
            build_seconds = time.perf_counter() - build_start
            sample = measure_search(
                searcher,
                queries,
                ground_truth,
                k,
                num_leaves,
                reorder,
            )
            sample["iteration"] = iteration + 1
            sample["build_time_s"] = float(build_seconds)
            samples.append(sample)

        results[distance] = {
            "distance_measure": distance,
            "aggregation": "median",
            "num_leaves": num_leaves,
            "dims_per_block": dims_per_block,
            "anisotropic_quantization_threshold": anisotropic_quantization_threshold,
            "reorder": reorder,
            "leaves_to_search": num_leaves,
            "samples": samples,
            "build_time_s": float(statistics.median(sample["build_time_s"] for sample in samples)),
            "qps": float(statistics.median(sample["qps"] for sample in samples)),
            "latency_per_query_us": float(
                statistics.median(sample["latency_per_query_us"] for sample in samples)
            ),
            "recall_at_k": float(
                statistics.median(sample["recall_at_k"] for sample in samples)
            ),
        }

    payload = {
        "benchmark": "ann_search",
        "software": "scann",
        "version": os.environ.get("SOFTWARE_VERSION", ""),
        "architecture": os.environ.get("EXPECTED_ARCH", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "parameters": {
            "dataset": "numpy_random_float32",
            "seed": seed,
            "data_scale": data_scale,
            "num_vectors": num_vectors,
            "dimension": dimension,
            "num_queries": num_queries,
            "k": k,
            "iterations": iterations,
            "aggregation": "median",
            "distance_measures": list(_DISTANCE_MEASURES),
            "num_leaves": num_leaves,
            "dims_per_block": dims_per_block,
            "anisotropic_quantization_threshold": anisotropic_quantization_threshold,
            "reorder": reorder,
            "leaves_to_search": num_leaves,
        },
        "results": results,
    }
    write_json(OUTPUT_FILE, payload)
    print(json.dumps({"benchmark_file": str(OUTPUT_FILE)}))


if __name__ == "__main__":
    main()
