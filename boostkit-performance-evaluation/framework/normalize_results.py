"""Validate software outputs and normalize their declared metrics."""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any

from build_info import load_build_info
from context import RunContext
from json_helper import atomic_write_json, get_path, load_json


class ResultValidationError(ValueError):
    """Raised when a software result violates the common result contract."""


MISSING = object()


def resolved_parameters(sources: dict[str, Any]) -> dict[str, Any]:
    """Collect the workload parameters recorded by declared JSON outputs."""
    return {
        source_name: source["parameters"]
        for source_name, source in sources.items()
        if isinstance(source, dict) and isinstance(source.get("parameters"), dict)
    }


def parameter_signature(context: RunContext, parameters: dict[str, Any]) -> str:
    payload = {
        "software": context.software,
        "version": context.version,
        "parameters": parameters,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _load_output(
    context: RunContext,
    output_name: str,
    definition: dict[str, Any],
) -> Any | None:
    filename = definition["path"]
    path = context.output_dir / filename
    output_format = definition["format"]
    if output_format == "directory":
        if not path.is_dir():
            if definition["required"]:
                raise ResultValidationError(
                    f"required output {output_name} is missing directory: {filename}"
                )
            return None
        file_count = sum(1 for entry in path.rglob("*") if entry.is_file())
        if file_count == 0:
            raise ResultValidationError(f"output {output_name} directory is empty: {filename}")
        return {"path": filename, "file_count": file_count}
    if not path.is_file():
        if definition["required"]:
            raise ResultValidationError(
                f"required output {output_name} is missing: {filename}"
            )
        return None
    if path.stat().st_size == 0:
        raise ResultValidationError(f"output {output_name} is empty: {filename}")
    if output_format == "json":
        try:
            value = load_json(path)
        except (OSError, json.JSONDecodeError) as exc:
            raise ResultValidationError(
                f"output {output_name} contains invalid JSON ({filename}): {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise ResultValidationError(
                f"output {output_name} JSON root must be an object: {filename}"
            )
        return value
    # Keep a portable result-directory-relative path.  The workflow downloads
    # artifacts into a different workspace before publishing history, so an
    # absolute runner path cannot be used to preserve declared text evidence.
    return {"path": filename, "size": path.stat().st_size}


def validate_stage_outputs(context: RunContext, stage: str) -> None:
    """Validate every output assigned to a completed software stage."""
    for output_name, definition in context.case.get("outputs", {}).items():
        if definition["stage"] == stage:
            _load_output(context, output_name, definition)


def _load_sources(context: RunContext) -> dict[str, Any]:
    sources: dict[str, Any] = {}
    for output_name, definition in context.case.get("outputs", {}).items():
        loaded = _load_output(context, output_name, definition)
        if loaded is None:
            continue
        sources[output_name] = loaded
    return sources


def _extract_metrics(context: RunContext, sources: dict[str, Any]) -> dict[str, Any]:
    metrics: dict[str, Any] = {}
    metric_config = context.case.get("metrics", {})
    default_source = metric_config.get("source")

    def add_metric(
        name: str,
        value: Any,
        unit: str,
        direction: str,
        group: str | None = None,
        matrix: dict[str, Any] | None = None,
    ) -> None:
        if not name:
            raise ResultValidationError("metric name must not be empty")
        if name in metrics:
            raise ResultValidationError(f"duplicate metric name: {name}")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ResultValidationError(
                f"metric {name} must be numeric, got {type(value).__name__}"
            )
        numeric_value = float(value)
        if not math.isfinite(numeric_value):
            raise ResultValidationError(f"metric {name} must be finite")
        metric = {
            "value": value,
            "unit": unit,
            "direction": direction,
        }
        if group is not None:
            if not isinstance(group, str) or not group:
                raise ResultValidationError(f"metric {name} has an invalid group")
            metric["group"] = group
        if matrix is not None:
            metric["matrix"] = matrix
        metrics[name] = metric

    for metric_name, definition in metric_config.get("definitions", {}).items():
        source_name = definition.get("source", default_source)
        if source_name not in sources:
            raise ResultValidationError(
                f"metric {metric_name} references unavailable source {source_name}"
            )
        value = get_path(sources[source_name], definition["path"], MISSING)
        if value is MISSING:
            raise ResultValidationError(
                f"metric {metric_name} path does not exist: "
                f"{source_name}:{definition['path']}"
            )
        add_metric(metric_name, value, definition["unit"], definition["direction"])

    collection = metric_config.get("collection")
    if isinstance(collection, dict):
        if default_source not in sources:
            raise ResultValidationError(
                f"metric collection references unavailable source {default_source}"
            )
        items = get_path(sources[default_source], collection["path"], MISSING)
        if items is MISSING:
            raise ResultValidationError(
                "metric collection path does not exist: "
                f"{default_source}:{collection['path']}"
            )
        if not isinstance(items, dict) or not items:
            raise ResultValidationError("metric collection must be a non-empty object")
        for item_key, item in items.items():
            if not isinstance(item, dict):
                raise ResultValidationError(
                    f"metric collection item {item_key} must be an object"
                )
            name_path = collection.get("name_path")
            metric_name = (
                get_path(item, name_path, MISSING) if name_path else item_key
            )
            if metric_name is MISSING:
                raise ResultValidationError(
                    f"metric collection item {item_key} is missing name path {name_path}"
                )
            if not isinstance(metric_name, str) or not metric_name:
                raise ResultValidationError(
                    f"metric collection item {item_key} has an invalid name"
                )
            value = get_path(item, collection["value_path"], MISSING)
            if value is MISSING:
                raise ResultValidationError(
                    f"metric {metric_name} path does not exist: "
                    f"{default_source}:{collection['path']}.{item_key}."
                    f"{collection['value_path']}"
                )
            unit = collection.get("unit")
            if unit is None:
                unit_path = collection["unit_path"]
                unit = get_path(item, unit_path, MISSING)
                if unit is MISSING:
                    raise ResultValidationError(
                        f"metric {metric_name} is missing unit path {unit_path}"
                    )
            if not isinstance(unit, str) or not unit:
                raise ResultValidationError(
                    f"metric {metric_name} must have a non-empty unit"
                )
            direction = collection.get("direction")
            if direction is None:
                direction_path = collection["direction_path"]
                direction = get_path(item, direction_path, MISSING)
                if direction is MISSING:
                    raise ResultValidationError(
                        f"metric {metric_name} is missing direction path {direction_path}"
                    )
            if direction not in {"higher_is_better", "lower_is_better", "neutral"}:
                raise ResultValidationError(
                    f"metric {metric_name} has invalid direction: {direction}"
                )
            group = None
            group_path = collection.get("group_path")
            if group_path is not None:
                group = get_path(item, group_path, MISSING)
                if group is MISSING:
                    raise ResultValidationError(
                        f"metric {metric_name} is missing group path {group_path}"
                    )
            matrix = None
            matrix_config = collection.get("matrix")
            if matrix_config is not None:
                group = get_path(item, matrix_config["group_path"], MISSING)
                row = get_path(item, matrix_config["row_path"], MISSING)
                column = get_path(item, matrix_config["column_path"], MISSING)
                if not isinstance(group, str) or not group:
                    raise ResultValidationError(f"metric {metric_name} has an invalid matrix group")
                if isinstance(row, bool) or not isinstance(row, (int, float, str)) or row == "":
                    raise ResultValidationError(f"metric {metric_name} has an invalid matrix row")
                if not isinstance(column, str) or not column:
                    raise ResultValidationError(f"metric {metric_name} has an invalid matrix column")
                matrix = {
                    "group": group,
                    "row": row,
                    "column": column,
                    "row_label": matrix_config["row_label"],
                    "column_order": matrix_config["column_order"],
                }
                note = matrix_config.get("single_note")
                if note is not None:
                    matrix["single_note"] = note
            add_metric(metric_name, value, unit, direction, group, matrix)
    if not metrics:
        raise ResultValidationError("no metrics were extracted")
    return metrics


def normalize(context: RunContext, command_status: str) -> dict[str, Any]:
    sources = _load_sources(context)
    parameters = resolved_parameters(sources)
    return {
        "software": context.software,
        "category": context.category,
        "version": context.version,
        "architecture": context.architecture,
        "run_id": context.run_id,
        "status": command_status,
        "cleanup_status": "pending",
        "build_info": load_build_info(context),
        "parameters": parameters,
        "parameter_signature": parameter_signature(context, parameters),
        "test_tools": context.case.get("test_tools", {}),
        "system_info": load_json(context.output_dir / "system_info.json", {}),
        "runtime_before": load_json(context.output_dir / "runtime_before.json", {}),
        "runtime_after": load_json(context.output_dir / "runtime_after.json", {}),
        "sources": sources,
        "metrics": _extract_metrics(context, sources),
    }


def write_normalized(context: RunContext, command_status: str) -> Path:
    output = context.output_dir / "normalized_result.json"
    atomic_write_json(output, normalize(context, command_status))
    return output
