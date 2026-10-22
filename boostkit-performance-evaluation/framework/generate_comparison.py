#!/usr/bin/env python3
"""Aggregate downloaded task artifacts and generate cross-architecture reports."""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from json_helper import atomic_write_json, load_json
from reporting import render_junit, render_summary


def _number(value: Any) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def compare_pair(x86: dict, arm: dict) -> dict:
    for field in ("category", "software", "version"):
        if x86.get(field) != arm.get(field):
            raise ValueError(f"result identity differs for {field}")
    if x86.get("parameters", {}) != arm.get("parameters", {}):
        raise ValueError("resolved workload parameters differ")
    if x86.get("parameter_signature") != arm.get("parameter_signature"):
        raise ValueError("parameter signatures differ")
    if x86.get("test_tools", {}) != arm.get("test_tools", {}):
        raise ValueError("test tools differ between architectures")
    x_metrics = x86.get("metrics", {})
    a_metrics = arm.get("metrics", {})
    if not isinstance(x_metrics, dict) or not isinstance(a_metrics, dict) or not x_metrics:
        raise ValueError("both architectures must contain metrics")
    metrics: dict[str, Any] = {}
    metric_names = list(x_metrics)
    metric_names.extend(name for name in a_metrics if name not in x_metrics)
    for name in metric_names:
        x_metric = x_metrics.get(name)
        a_metric = a_metrics.get(name)
        for architecture, metric in (("x86_64", x_metric), ("aarch64", a_metric)):
            if metric is not None and not isinstance(metric, dict):
                raise ValueError(f"metric {name} for {architecture} must be an object")
        present_metrics = [metric for metric in (x_metric, a_metric) if metric is not None]
        if not present_metrics:
            raise ValueError(f"metric {name} has no architecture value")
        units = {metric.get("unit") for metric in present_metrics}
        if len(units) != 1:
            raise ValueError(f"metric {name} units differ between architectures")
        directions = {metric.get("direction") for metric in present_metrics}
        if len(directions) != 1:
            raise ValueError(f"metric {name} directions differ between architectures")
        groups = {metric.get("group") for metric in present_metrics}
        if len(groups) != 1:
            raise ValueError(f"metric {name} groups differ between architectures")
        matrices = {json.dumps(metric.get("matrix"), sort_keys=True) for metric in present_metrics}
        if len(matrices) != 1:
            raise ValueError(f"metric {name} matrix declarations differ between architectures")
        x_value = _number(x_metric.get("value")) if x_metric is not None else None
        a_value = _number(a_metric.get("value")) if a_metric is not None else None
        if (
            (x_value is not None and not math.isfinite(x_value))
            or (a_value is not None and not math.isfinite(a_value))
        ):
            raise ValueError(f"metric {name} must contain finite numeric values")
        direction = next(iter(directions))
        raw_ratio = (
            a_value / x_value
            if x_value not in (None, 0) and a_value is not None
            else None
        )
        relative = None
        if raw_ratio is not None:
            if direction == "higher_is_better":
                relative = raw_ratio
            elif direction == "lower_is_better" and raw_ratio != 0:
                relative = 1 / raw_ratio
        metric_data = {
            "x86_64": x_metric.get("value") if x_metric is not None else None,
            "aarch64": a_metric.get("value") if a_metric is not None else None,
            "unit": next(iter(units)),
            "direction": direction,
            "raw_ratio": round(raw_ratio, 4) if raw_ratio is not None else None,
            "relative_performance": round(relative, 4) if relative is not None else None,
        }
        group = next(iter(groups))
        if group is not None:
            if not isinstance(group, str) or not group:
                raise ValueError(f"metric {name} has an invalid group")
            metric_data["group"] = group
        matrix = x_metric.get("matrix") if x_metric is not None else a_metric.get("matrix")
        if matrix is not None:
            if not isinstance(matrix, dict):
                raise ValueError(f"metric {name} has an invalid matrix declaration")
            metric_data["matrix"] = matrix
        metrics[name] = metric_data
    return {
        "category": arm["category"],
        "software": arm["software"],
        "version": arm["version"],
        "parameter_signature": arm.get("parameter_signature"),
        "test_tools": arm.get("test_tools", {}),
        "environments": {
            architecture: {
                field: result.get(field, {})
                for field in (
                    "build_info",
                    "system_info",
                    "runtime_before",
                    "runtime_after",
                )
            }
            for architecture, result in (("x86_64", x86), ("aarch64", arm))
        },
        "metrics": metrics,
    }


def build_summary(results: list[dict], comparisons: list[dict]) -> dict:
    """Build the shared model for workflow and permanent summary reports."""
    items = [
        {
            "category": result.get("category"),
            "software": result.get("software"),
            "version": result.get("version"),
            "architecture": result.get("architecture"),
            "status": result.get("status"),
            "cleanup_status": result.get("cleanup_status", "unknown"),
            "build_info": result.get("build_info", {}),
            "system_info": result.get("system_info", {}),
            "runtime_before": result.get("runtime_before", {}),
            "runtime_after": result.get("runtime_after", {}),
            "test_tools": result.get("test_tools", {}),
            "metrics": result.get("metrics", {}),
        }
        for result in results
    ]
    return {
        "total": len(results),
        "passed": sum(1 for result in results if result.get("status") == "passed"),
        "failed": sum(1 for result in results if result.get("status") != "passed"),
        "comparisons": len(comparisons),
        "items": items,
    }


def generate(input_root: Path, output_dir: Path) -> dict:
    normalized_paths = sorted(input_root.rglob("normalized_result.json"))
    normalized_dirs = {path.parent.resolve() for path in normalized_paths}
    results = [load_json(path, {}) for path in normalized_paths]
    for status_path in sorted(input_root.rglob("status.json")):
        if status_path.parent.resolve() in normalized_dirs:
            continue
        status = load_json(status_path, {})
        if status:
            results.append({
                "category": status.get("category", "unknown"),
                "software": status.get("software", "unknown"),
                "version": status.get("version", "unknown"),
                "architecture": status.get("architecture", "unknown"),
                "run_id": status.get("run_id"),
                "status": status.get("status", "failed"),
                "failed_stage": status.get("failed_stage"),
                "cleanup_status": status.get("cleanup_status"),
                "metrics": {},
            })
    results = [result for result in results if result]
    grouped: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for result in results:
        key = (result.get("category"), result.get("software"), result.get("version"))
        grouped[key][result.get("architecture")] = result

    output_dir.mkdir(parents=True, exist_ok=True)
    comparisons: list[dict] = []
    for key, architectures in sorted(grouped.items()):
        if "x86_64" not in architectures or "aarch64" not in architectures:
            continue
        x86 = architectures["x86_64"]
        arm = architectures["aarch64"]
        if any(
            result.get("status") != "passed" or result.get("cleanup_status") != "passed"
            for result in (x86, arm)
        ):
            continue
        comparison = compare_pair(x86, arm)
        comparisons.append(comparison)
        stem = f"{comparison['category']}-{comparison['software']}-{comparison['version']}"
        atomic_write_json(output_dir / f"{stem}.json", comparison)

    summary = build_summary(results, comparisons)
    atomic_write_json(
        output_dir / "combined-report.json",
        {"summary": summary, "comparisons": comparisons},
    )
    (output_dir / "combined-report.md").write_text(
        render_summary(summary, comparisons), encoding="utf-8"
    )
    (output_dir / "junit.xml").write_text(render_junit(results), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = generate(args.input_root, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
