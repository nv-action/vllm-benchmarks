#!/usr/bin/env python3
"""Validate the software catalog and generate the execution matrix."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

from build_info import BUILD_INFO_FILENAME

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ARCHITECTURES = ("x86_64", "aarch64")
SOFTWARE_STAGES = ("build", "start", "test", "stop")
VALID_DIRECTIONS = {"higher_is_better", "lower_is_better", "neutral"}
VALID_OUTPUT_FORMATS = {"json", "text", "binary", "directory"}
SHELL_FUNCTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OUTPUT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a mapping")
    return data


def configured_software(root: Path = ROOT) -> dict[str, list[str]]:
    data = load_yaml(root / "config" / "categories.yaml")
    categories = data.get("categories")
    if not isinstance(categories, dict) or not categories:
        raise ValueError("config/categories.yaml must contain a non-empty categories mapping")
    registry: dict[str, list[str]] = {}
    software_categories: dict[str, str] = {}
    for category, configured_names in categories.items():
        if not isinstance(category, str) or not category:
            raise ValueError("category names must be non-empty strings")
        if Path(category).parts != (category,) or category in {".", ".."}:
            raise ValueError(f"category {category!r} must be a single directory name")
        if configured_names is None:
            names: list[str] = []
        elif isinstance(configured_names, list) and all(
            isinstance(name, str) and name for name in configured_names
        ):
            names = list(configured_names)
        else:
            raise ValueError(
                f"category {category} must be empty or contain a string software list"
            )
        if len(names) != len(set(names)):
            raise ValueError(f"category {category} must not contain duplicate software names")
        for name in names:
            if Path(name).parts != (name,) or name in {".", ".."}:
                raise ValueError(f"software {name!r} must be a single directory name")
            if name in software_categories:
                raise ValueError(
                    f"software {name!r} is registered in both "
                    f"{software_categories[name]} and {category}"
                )
            software_categories[name] = category
        registry[category] = names
    return registry


def configured_categories(root: Path = ROOT) -> list[str]:
    return list(configured_software(root))


def configured_runner_labels(
    root: Path = ROOT, runner_profile: str = "production"
) -> dict[str, str]:
    defaults = load_yaml(root / "config" / "defaults.yaml")
    profiles = defaults.get("runner_labels")
    if not isinstance(profiles, dict):
        raise ValueError("config/defaults.yaml must define runner_labels")
    labels = profiles.get(runner_profile)
    if not isinstance(labels, dict):
        raise ValueError(f"runner profile is not configured: {runner_profile}")
    missing = set(DEFAULT_ARCHITECTURES) - set(labels)
    if missing:
        raise ValueError(
            f"runner labels not configured for {runner_profile}: "
            f"{', '.join(sorted(missing))}"
        )
    if not all(
        isinstance(labels[architecture], str) and labels[architecture]
        for architecture in DEFAULT_ARCHITECTURES
    ):
        raise ValueError("runner labels must be non-empty strings")
    return {architecture: labels[architecture] for architecture in DEFAULT_ARCHITECTURES}


def discover_cases(root: Path = ROOT) -> list[Path]:
    return sorted((root / "software").glob("*/*/case.yaml"))


def validate_case(path: Path, root: Path = ROOT) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        case = load_yaml(path)
    except Exception as exc:
        return None, [f"cannot load YAML: {exc}"]

    required = ("name", "category", "enabled", "versions", "execution", "outputs", "metrics")
    for field in required:
        if field not in case:
            errors.append(f"missing required field: {field}")
    unknown_fields = set(case) - set(required) - {
        "test_tools", "environment", "version_overrides",
        "architectures", "runner", "runner_label",
    }
    if unknown_fields:
        errors.append(
            "case contains unsupported fields: "
            + ", ".join(sorted(str(field) for field in unknown_fields))
        )
    for field in ("environment", "version_overrides"):
        if field in case:
            errors.append(f"{field} is not supported; configure software defaults in its script")

    name = case.get("name")
    category = case.get("category")
    if not isinstance(name, str) or not name:
        errors.append("name must be a non-empty string")
    try:
        categories = configured_categories(root)
    except (OSError, ValueError) as exc:
        errors.append(str(exc))
        categories = []
    if category not in categories:
        errors.append(f"category must be one of {categories}")

    try:
        relative = path.resolve().relative_to((root / "software").resolve())
        path_category, path_software = relative.parts[0], relative.parts[1]
        if category != path_category:
            errors.append(f"category {category!r} does not match directory {path_category!r}")
        if name != path_software:
            errors.append(f"name {name!r} does not match directory {path_software!r}")
    except (ValueError, IndexError):
        errors.append("case.yaml must be at software/<category>/<software>/case.yaml")

    versions = case.get("versions")
    if (
        not isinstance(versions, list)
        or not versions
        or not all(isinstance(value, str) and value for value in versions)
    ):
        errors.append("versions must be a non-empty list of strings")
    elif len(versions) != len(set(versions)):
        errors.append("versions must not contain duplicates")

    test_tools = case.get("test_tools")
    if test_tools is not None:
        if not isinstance(test_tools, dict) or not test_tools:
            errors.append("test_tools must be a non-empty mapping when declared")
        else:
            for tool_name, definition in test_tools.items():
                if not isinstance(tool_name, str) or not tool_name:
                    errors.append("test_tools names must be non-empty strings")
                    continue
                if not isinstance(definition, dict):
                    errors.append(f"test_tools.{tool_name} must be a mapping")
                    continue
                unknown_fields = set(definition) - {"version", "revision"}
                if unknown_fields:
                    errors.append(
                        f"test_tools.{tool_name} contains unsupported fields: "
                        f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                    )
                if not isinstance(definition.get("version"), str) or not definition["version"]:
                    errors.append(
                        f"test_tools.{tool_name}.version must be a non-empty string"
                    )
                revision = definition.get("revision")
                if revision is not None and (
                    not isinstance(revision, str) or not revision
                ):
                    errors.append(
                        f"test_tools.{tool_name}.revision must be a non-empty string"
                    )

    execution = case.get("execution")
    if not isinstance(execution, dict):
        errors.append("execution must be a mapping")
    else:
        unknown_fields = set(execution) - {
            "type", "stages", "timeout_minutes", "interface", "entrypoint",
            "expected_outputs", "environment",
        }
        if unknown_fields:
            errors.append(
                "execution contains unsupported fields: "
                + ", ".join(sorted(str(field) for field in unknown_fields))
            )
        if execution.get("type") != "shell-functions":
            errors.append("execution.type must be shell-functions")
        for legacy_field in ("interface", "entrypoint"):
            if legacy_field in execution:
                errors.append(f"execution.{legacy_field} is not allowed; declare execution.stages")
        stages = execution.get("stages")
        if not isinstance(stages, dict):
            errors.append("execution.stages must be a mapping")
        else:
            stage_names = set(stages)
            missing_stages = set(SOFTWARE_STAGES) - stage_names
            unknown_stages = stage_names - set(SOFTWARE_STAGES)
            if missing_stages:
                errors.append(
                    f"execution.stages is missing: {', '.join(sorted(missing_stages))}"
                )
            if unknown_stages:
                errors.append(
                    f"execution.stages contains unsupported stages: "
                    f"{', '.join(sorted(str(stage) for stage in unknown_stages))}"
                )
            for stage in SOFTWARE_STAGES:
                if stage not in stages:
                    continue
                definition = stages[stage]
                if not isinstance(definition, dict):
                    errors.append(f"execution.stages.{stage} must be a mapping")
                    continue
                unknown_fields = set(definition) - {"script", "function"}
                if unknown_fields:
                    errors.append(
                        f"execution.stages.{stage} contains unsupported fields: "
                        f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                    )
                script = definition.get("script")
                function = definition.get("function")
                if not isinstance(script, str) or not script:
                    errors.append(f"execution.stages.{stage}.script must be a non-empty string")
                else:
                    script_path = Path(script)
                    if script_path.is_absolute() or ".." in script_path.parts:
                        errors.append(
                            f"execution.stages.{stage}.script must remain inside "
                            "the software directory"
                        )
                    elif script_path.suffix != ".sh":
                        errors.append(f"execution.stages.{stage}.script must be a Shell script")
                    else:
                        declared_script = path.parent / script_path
                        try:
                            declared_script.resolve().relative_to(path.parent.resolve())
                        except ValueError:
                            errors.append(
                                f"execution.stages.{stage}.script must remain inside "
                                "the software directory"
                            )
                        else:
                            if not declared_script.is_file():
                                errors.append(
                                    f"execution.stages.{stage}.script does not exist: {script}"
                                )
                if not isinstance(function, str) or not SHELL_FUNCTION_PATTERN.fullmatch(function):
                    errors.append(
                        f"execution.stages.{stage}.function must be a valid Shell function name"
                    )
        if "expected_outputs" in execution:
            errors.append("execution.expected_outputs is not allowed; declare top-level outputs")
        timeout = execution.get("timeout_minutes")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("execution.timeout_minutes must be a positive integer")
        if "environment" in execution:
            errors.append("execution.environment is not supported; configure software defaults in its script")

    output_by_name: dict[str, dict[str, Any]] = {}
    output_paths: set[str] = set()
    outputs = case.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        errors.append("outputs must be a non-empty mapping")
    else:
        for output_name, definition in outputs.items():
            if not isinstance(output_name, str) or not OUTPUT_NAME_PATTERN.fullmatch(output_name):
                errors.append(
                    "output names must use lowercase letters, numbers, and underscores"
                )
                continue
            if not isinstance(definition, dict):
                errors.append(f"output {output_name} must be a mapping")
                continue
            output_by_name[output_name] = definition
            unknown_fields = set(definition) - {"path", "stage", "format", "required"}
            if unknown_fields:
                errors.append(
                    f"output {output_name} contains unsupported fields: "
                    f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                )
            missing_fields = {
                field for field in ("path", "stage", "format", "required")
                if field not in definition
            }
            if missing_fields:
                errors.append(
                    f"output {output_name} is missing fields: "
                    f"{', '.join(sorted(missing_fields))}"
                )
            output_path = definition.get("path")
            if not isinstance(output_path, str) or not output_path:
                errors.append(f"output {output_name}.path must be a non-empty string")
            else:
                path_value = Path(output_path)
                if path_value.is_absolute() or ".." in path_value.parts:
                    errors.append(
                        f"output {output_name}.path must remain inside the result directory"
                    )
                elif path_value == Path(BUILD_INFO_FILENAME):
                    errors.append(
                        f"output {output_name}.path is Framework-reserved: "
                        f"{BUILD_INFO_FILENAME}"
                    )
                elif output_path in output_paths:
                    errors.append(
                        f"output path {output_path} is declared by more than one output"
                    )
                else:
                    output_paths.add(output_path)
            if definition.get("stage") not in SOFTWARE_STAGES:
                errors.append(
                    f"output {output_name}.stage must be one of {list(SOFTWARE_STAGES)}"
                )
            output_format = definition.get("format")
            if output_format not in VALID_OUTPUT_FORMATS:
                errors.append(
                    f"output {output_name}.format must be one of "
                    f"{sorted(VALID_OUTPUT_FORMATS)}"
                )
            elif (
                output_format == "json"
                and isinstance(output_path, str)
                and not output_path.endswith(".json")
            ):
                errors.append(f"output {output_name} with json format must use a .json path")
            if not isinstance(definition.get("required"), bool):
                errors.append(f"output {output_name}.required must be boolean")

    metrics = case.get("metrics")
    if not isinstance(metrics, dict) or not metrics:
        errors.append("metrics must be a non-empty mapping")
    else:
        unknown_metric_fields = set(metrics) - {"source", "definitions", "collection"}
        if unknown_metric_fields:
            errors.append(
                "metrics contains unsupported fields: "
                f"{', '.join(sorted(str(field) for field in unknown_metric_fields))}"
            )
        default_source = metrics.get("source")
        if default_source is not None and (
            not isinstance(default_source, str) or not default_source
        ):
            errors.append("metrics.source must be a non-empty output name")
        elif isinstance(default_source, str):
            if default_source not in output_by_name:
                errors.append("metrics.source is not a declared output name")
            elif output_by_name[default_source].get("format") != "json":
                errors.append("metrics.source must reference a JSON output")
            elif output_by_name[default_source].get("required") is not True:
                errors.append("metrics.source must reference a required output")
        definitions = metrics.get("definitions")
        collection = metrics.get("collection")
        if (definitions is None) == (collection is None):
            errors.append("metrics must define exactly one of definitions or collection")
        if definitions is None:
            definitions = {}
        elif not isinstance(definitions, dict) or not definitions:
            errors.append("metrics.definitions must be a non-empty mapping")
            definitions = {}
        for metric_name, metric in definitions.items():
            if not isinstance(metric_name, str) or not metric_name:
                errors.append("metric names must be non-empty strings")
                continue
            if not isinstance(metric, dict):
                errors.append(f"metric {metric_name} must be a mapping")
                continue
            unknown_fields = set(metric) - {"source", "path", "unit", "direction"}
            if unknown_fields:
                errors.append(
                    f"metric {metric_name} contains unsupported fields: "
                    f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                )
            if metric.get("direction") not in VALID_DIRECTIONS:
                errors.append(f"metric {metric_name} has invalid direction")
            if not isinstance(metric.get("unit"), str) or not metric.get("unit"):
                errors.append(f"metric {metric_name} must define a non-empty unit")
            source = metric.get("source", default_source)
            if not isinstance(source, str) or not source:
                errors.append(f"metric {metric_name} must define source or use metrics.source")
            elif source not in output_by_name:
                errors.append(f"metric {metric_name} source is not a declared output name")
            elif output_by_name[source].get("format") != "json":
                errors.append(f"metric {metric_name} source must be a JSON output")
            elif output_by_name[source].get("required") is not True:
                errors.append(f"metric {metric_name} source must be a required output")
            if not isinstance(metric.get("path"), str) or not metric.get("path"):
                errors.append(f"metric {metric_name} must define path")
        if collection is not None:
            if not isinstance(collection, dict):
                errors.append("metrics.collection must be a mapping")
            else:
                unknown_fields = set(collection) - {
                    "path",
                    "name_path",
                    "value_path",
                    "unit",
                    "unit_path",
                    "direction",
                    "direction_path",
                    "group_path",
                    "matrix",
                }
                if unknown_fields:
                    errors.append(
                        "metrics.collection contains unsupported fields: "
                        f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                    )
                for field in ("path", "value_path"):
                    if not isinstance(collection.get(field), str) or not collection.get(field):
                        errors.append(
                            f"metrics.collection.{field} must be a non-empty string"
                        )
                name_path = collection.get("name_path")
                if name_path is not None and (
                    not isinstance(name_path, str) or not name_path
                ):
                    errors.append(
                        "metrics.collection.name_path must be a non-empty string"
                    )
                group_path = collection.get("group_path")
                if group_path is not None and (
                    not isinstance(group_path, str) or not group_path
                ):
                    errors.append(
                        "metrics.collection.group_path must be a non-empty string"
                    )
                matrix = collection.get("matrix")
                if matrix is not None:
                    if not isinstance(matrix, dict):
                        errors.append("metrics.collection.matrix must be a mapping")
                    else:
                        unknown_fields = set(matrix) - {
                            "group_path", "row_path", "column_path", "row_label",
                            "column_order", "single_note",
                        }
                        if unknown_fields:
                            errors.append(
                                "metrics.collection.matrix contains unsupported fields: "
                                f"{', '.join(sorted(str(field) for field in unknown_fields))}"
                            )
                        for field in ("group_path", "row_path", "column_path", "row_label"):
                            if not isinstance(matrix.get(field), str) or not matrix[field]:
                                errors.append(
                                    f"metrics.collection.matrix.{field} must be a non-empty string"
                                )
                        column_order = matrix.get("column_order")
                        if (
                            not isinstance(column_order, list)
                            or not column_order
                            or not all(isinstance(value, str) and value for value in column_order)
                            or len(column_order) != len(set(column_order))
                        ):
                            errors.append(
                                "metrics.collection.matrix.column_order must be a non-empty unique string list"
                            )
                        note = matrix.get("single_note")
                        if note is not None and (not isinstance(note, str) or not note):
                            errors.append(
                                "metrics.collection.matrix.single_note must be a non-empty string"
                            )
                unit = collection.get("unit")
                unit_path = collection.get("unit_path")
                if (unit is None) == (unit_path is None):
                    errors.append(
                        "metrics.collection must define exactly one of unit or unit_path"
                    )
                elif unit is not None and (not isinstance(unit, str) or not unit):
                    errors.append("metrics.collection.unit must be a non-empty string")
                elif unit_path is not None and (
                    not isinstance(unit_path, str) or not unit_path
                ):
                    errors.append(
                        "metrics.collection.unit_path must be a non-empty string"
                    )
                direction = collection.get("direction")
                direction_path = collection.get("direction_path")
                if (direction is None) == (direction_path is None):
                    errors.append(
                        "metrics.collection must define exactly one of direction or direction_path"
                    )
                elif direction is not None and direction not in VALID_DIRECTIONS:
                    errors.append("metrics.collection has invalid direction")
                elif direction_path is not None and (
                    not isinstance(direction_path, str) or not direction_path
                ):
                    errors.append(
                        "metrics.collection.direction_path must be a non-empty string"
                    )
                if not isinstance(default_source, str) or not default_source:
                    errors.append("metrics.collection requires metrics.source")

    for forbidden in ("architectures", "runner", "runner_label"):
        if forbidden in case:
            errors.append(f"{forbidden} must not be configured in a software manifest")
    return case, errors


def validate_catalog(root: Path = ROOT) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    try:
        registry = configured_software(root)
    except (OSError, ValueError) as exc:
        return [], [f"{root / 'config' / 'categories.yaml'}: {exc}"]

    registered_paths: list[Path] = []
    for category, software_names in registry.items():
        for software_name in software_names:
            registered_paths.append(
                root / "software" / category / software_name / "case.yaml"
            )
    registered_set = {path.resolve() for path in registered_paths}
    actual_paths = discover_cases(root)
    actual_set = {path.resolve() for path in actual_paths}

    for path in registered_paths:
        if path.resolve() not in actual_set:
            errors.append(f"registered case is missing: {path}")
            continue
        case, case_errors = validate_case(path, root)
        if case is not None:
            entries.append((path, case))
        errors.extend(f"{path}: {error}" for error in case_errors)

    for path in actual_paths:
        if path.resolve() not in registered_set:
            errors.append(
                f"case is not registered in config/categories.yaml: {path}"
            )
    return entries, errors


def build_matrix(
    software: str,
    version: str,
    architecture: str,
    runner_profile: str = "production",
) -> dict:
    entries, errors = validate_catalog(ROOT)
    if errors:
        raise ValueError("invalid software catalog: " + "; ".join(errors))
    selected_software = (
        None
        if software == "all"
        else {value.strip() for value in software.split(",") if value.strip()}
    )
    selected_versions = (
        None
        if version == "all"
        else {value.strip() for value in version.split(",") if value.strip()}
    )
    architectures = DEFAULT_ARCHITECTURES if architecture == "all" else (architecture,)
    runner_labels = configured_runner_labels(runner_profile=runner_profile)
    include: list[dict[str, Any]] = []
    matched_software: set[str] = set()
    matched_versions: set[str] = set()

    for path, case in entries:
        if not case.get("enabled", False):
            continue
        name = case["name"]
        if selected_software is not None and name not in selected_software:
            continue
        matched_software.add(name)
        for case_version in case["versions"]:
            if selected_versions is not None and case_version not in selected_versions:
                continue
            matched_versions.add(case_version)
            timeout = int(case["execution"].get("timeout_minutes", 180))
            for selected_architecture in architectures:
                include.append({
                    "category": case["category"],
                    "software": name,
                    "version": case_version,
                    "arch": selected_architecture,
                    "runner_label": runner_labels[selected_architecture],
                    "timeout_minutes": timeout,
                    "job_timeout_minutes": timeout + 30,
                    "case_path": str(path.relative_to(ROOT)),
                })

    if selected_software is not None:
        missing = selected_software - matched_software
        if missing:
            raise ValueError(f"unknown or disabled software: {', '.join(sorted(missing))}")
    if selected_versions is not None:
        missing = selected_versions - matched_versions
        if missing:
            raise ValueError(f"selected versions not found: {', '.join(sorted(missing))}")
    if not include:
        raise ValueError("selection produced an empty matrix")
    return {"include": include}


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate")
    matrix_parser = subparsers.add_parser("matrix")
    matrix_parser.add_argument("--software", default="all")
    matrix_parser.add_argument("--version", default="all")
    matrix_parser.add_argument(
        "--architecture", choices=("all", *DEFAULT_ARCHITECTURES), default="all"
    )
    matrix_parser.add_argument("--runner-profile", default="production")
    matrix_parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    if args.command == "validate":
        entries, errors = validate_catalog(ROOT)
        if errors:
            for error in errors:
                print(f"ERROR {error}", file=sys.stderr)
            return 1
        if not entries:
            print("OK no case manifests found; software catalog is empty")
        for path, _case in entries:
            print(f"OK {path}")
        return 0
    try:
        matrix = build_matrix(
            args.software,
            args.version,
            args.architecture,
            args.runner_profile,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            matrix,
            indent=2 if args.pretty else None,
            separators=None if args.pretty else (",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
