#!/usr/bin/env python3
"""Validate the functional test catalog and generate the environment matrix."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
CASES_DIRNAME = "functional-cases"
CONFIG_RELATIVE = Path("config") / "functional.yaml"
FUNC_STAGES = ("prepare", "install", "check", "cleanup")
VALID_OUTPUT_FORMATS = {"json", "text"}
VALID_RUNTIMES = {"native", "container"}
VALID_MODES = {"prebuilt", "build"}
SHELL_FUNCTION_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
OUTPUT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]*$")
CHECK_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_-]*$")


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    if not isinstance(data, dict):
        raise ValueError("manifest root must be a mapping")
    return data


def load_config(root: Path = ROOT) -> dict[str, Any]:
    """Load and validate config/functional.yaml into normalized structures."""
    data = load_yaml(root / CONFIG_RELATIVE)

    architectures = data.get("architectures")
    if (
        not isinstance(architectures, list)
        or not architectures
        or not all(isinstance(value, str) and value for value in architectures)
        or len(set(architectures)) != len(architectures)
    ):
        raise ValueError("architectures must be a non-empty unique string list")

    def index_by_name(entries: object, field: str) -> dict[str, dict[str, Any]]:
        if not isinstance(entries, list) or not entries:
            raise ValueError(f"{field} must be a non-empty list")
        indexed: dict[str, dict[str, Any]] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                raise ValueError(f"{field} entries must be mappings")
            name = entry.get("name")
            if not isinstance(name, str) or not name:
                raise ValueError(f"{field}.name must be a non-empty string")
            if name in indexed:
                raise ValueError(f"{field} contains duplicate name: {name}")
            indexed[name] = entry
        return indexed

    operating_systems = index_by_name(data.get("operating_systems"), "operating_systems")
    for name, entry in operating_systems.items():
        runtime = entry.get("runtime")
        if runtime not in VALID_RUNTIMES:
            raise ValueError(
                f"operating_systems.{name}.runtime must be one of {sorted(VALID_RUNTIMES)}"
            )
        if runtime == "container":
            mode = entry.get("mode")
            if mode not in VALID_MODES:
                raise ValueError(
                    f"operating_systems.{name}.mode must be one of {sorted(VALID_MODES)}"
                )
            if mode == "prebuilt":
                image_template = entry.get("image_template", "")
                if not isinstance(image_template, str) or not image_template:
                    raise ValueError(
                        f"operating_systems.{name}.image_template is required "
                        "for container/prebuilt mode"
                    )
                if "{software}" not in image_template or "{version}" not in image_template:
                    raise ValueError(
                        f"operating_systems.{name}.image_template must contain "
                        "{software} and {version}"
                    )
            else:
                image = entry.get("image", "")
                if not isinstance(image, str) or not image:
                    raise ValueError(
                        f"operating_systems.{name}.image is required for container/build mode"
                    )

    hardware = index_by_name(data.get("hardware"), "hardware")
    for name, entry in hardware.items():
        labels = entry.get("runner_labels")
        if (
            not isinstance(labels, list)
            or not labels
            or not all(isinstance(label, str) and label for label in labels)
            or len(set(labels)) != len(labels)
        ):
            raise ValueError(
                f"hardware.{name}.runner_labels must be a non-empty unique string list"
            )

    output_root = data.get("output_root")
    work_root = data.get("work_root")
    if not isinstance(output_root, str) or not output_root:
        raise ValueError("output_root must be a non-empty string")
    if not isinstance(work_root, str) or not work_root:
        raise ValueError("work_root must be a non-empty string")

    return {
        "architectures": list(architectures),
        "operating_systems": {
            name: {
                "name": name,
                "runtime": entry["runtime"],
                "mode": entry.get("mode", ""),
                "image_template": entry.get("image_template", ""),
                "image": entry.get("image", ""),
            }
            for name, entry in operating_systems.items()
        },
        "hardware": {
            name: {"name": name, "runner_labels": list(entry["runner_labels"])}
            for name, entry in hardware.items()
        },
        "output_root": output_root,
        "work_root": work_root,
    }


def compose_image(image_template: str, software: str, version: str, arch: str) -> str:
    """Render the per-software image, e.g. 'openeulertest/{software}:{version}-oe2403sp4-{arch}'."""
    return image_template.format(software=software, version=version, arch=arch)


def discover_cases(root: Path = ROOT) -> list[Path]:
    return sorted((root / CASES_DIRNAME).glob("*/*/func_case.yaml"))


def _validate_checks(checks: object, errors: list[str]) -> None:
    if not isinstance(checks, list) or not checks:
        errors.append("checks must be a non-empty list")
        return
    seen: set[str] = set()
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            errors.append(f"checks[{index}] must be a mapping")
            continue
        name = check.get("name")
        if not isinstance(name, str) or not CHECK_NAME_PATTERN.fullmatch(name):
            errors.append(f"checks[{index}].name must be a lowercase identifier")
            continue
        if name in seen:
            errors.append(f"checks contains duplicate name: {name}")
        seen.add(name)
        for optional in ("description", "upstream_command"):
            if optional in check and (
                not isinstance(check[optional], str) or not check[optional]
            ):
                errors.append(f"checks[{index}].{optional} must be a non-empty string")
        unknown = set(check) - {"name", "description", "upstream_command"}
        if unknown:
            errors.append(
                f"checks[{index}] contains unsupported fields: "
                f"{', '.join(sorted(str(field) for field in unknown))}"
            )


def validate_case(
    path: Path, root: Path = ROOT
) -> tuple[dict[str, Any] | None, list[str]]:
    errors: list[str] = []
    try:
        case = load_yaml(path)
    except Exception as exc:
        return None, [f"cannot load YAML: {exc}"]

    required = ("name", "category", "enabled", "versions", "execution", "checks", "outputs")
    for field in required:
        if field not in case:
            errors.append(f"missing required field: {field}")

    if "architectures" in case:
        errors.append("architectures must not be configured in a software manifest")

    name = case.get("name")
    category = case.get("category")
    if not isinstance(name, str) or not name:
        errors.append("name must be a non-empty string")
    if not isinstance(category, str) or not category:
        errors.append("category must be a non-empty string")
    # 可选:预构建镜像仓库名与用例目录名不一致时(如 golang -> go)在此声明。
    image_name = case.get("image_name")
    if image_name is not None and (
        not isinstance(image_name, str) or not image_name or "/" in image_name
    ):
        errors.append("image_name must be a non-empty repository name without '/'")
    # 可选:build 模式下从该预构建镜像导出根文件系统,供无对应发行版二进制的 OS 复用。
    bootstrap_image = case.get("bootstrap_image")
    if bootstrap_image is not None:
        if not isinstance(bootstrap_image, str) or not bootstrap_image:
            errors.append("bootstrap_image must be a non-empty string")
        else:
            try:
                compose_image(
                    bootstrap_image,
                    image_name if isinstance(image_name, str) and image_name else (name or "software"),
                    "0.0.0",
                    "aarch64",
                )
            except (KeyError, IndexError, ValueError):
                errors.append("bootstrap_image contains unsupported placeholders")

    try:
        relative = path.resolve().relative_to((root / CASES_DIRNAME).resolve())
        path_category, path_software = relative.parts[0], relative.parts[1]
        if category != path_category:
            errors.append(f"category {category!r} does not match directory {path_category!r}")
        if name != path_software:
            errors.append(f"name {name!r} does not match directory {path_software!r}")
    except (ValueError, IndexError):
        errors.append(f"func_case.yaml must be at {CASES_DIRNAME}/<category>/<software>/func_case.yaml")

    versions = case.get("versions")
    if (
        not isinstance(versions, list)
        or not versions
        or not all(isinstance(value, str) and value for value in versions)
    ):
        errors.append("versions must be a non-empty list of strings")
    elif len(versions) != len(set(versions)):
        errors.append("versions must not contain duplicates")

    execution = case.get("execution")
    if not isinstance(execution, dict):
        errors.append("execution must be a mapping")
    else:
        unknown = set(execution) - {"type", "stages", "timeout_minutes", "min_free_disk_gb"}
        if unknown:
            errors.append(
                "execution contains unsupported fields: "
                + ", ".join(sorted(str(field) for field in unknown))
            )
        if execution.get("type") != "shell-functions":
            errors.append("execution.type must be shell-functions")
        stages = execution.get("stages")
        if not isinstance(stages, dict):
            errors.append("execution.stages must be a mapping")
        else:
            missing = set(FUNC_STAGES) - set(stages)
            unknown_stages = set(stages) - set(FUNC_STAGES)
            if missing:
                errors.append(f"execution.stages is missing: {', '.join(sorted(missing))}")
            if unknown_stages:
                errors.append(
                    "execution.stages contains unsupported stages: "
                    + ", ".join(sorted(str(stage) for stage in unknown_stages))
                )
            for stage in FUNC_STAGES:
                if stage not in stages:
                    continue
                definition = stages[stage]
                if not isinstance(definition, dict):
                    errors.append(f"execution.stages.{stage} must be a mapping")
                    continue
                unknown = set(definition) - {"script", "function"}
                if unknown:
                    errors.append(
                        f"execution.stages.{stage} contains unsupported fields: "
                        + ", ".join(sorted(str(field) for field in unknown))
                    )
                script = definition.get("script")
                function = definition.get("function")
                if not isinstance(script, str) or not script:
                    errors.append(
                        f"execution.stages.{stage}.script must be a non-empty string"
                    )
                else:
                    script_path = Path(script)
                    if script_path.is_absolute() or ".." in script_path.parts:
                        errors.append(
                            f"execution.stages.{stage}.script must remain inside "
                            "the software directory"
                        )
                    elif script_path.suffix != ".sh":
                        errors.append(f"execution.stages.{stage}.script must be a Shell script")
                    elif not (path.parent / script_path).is_file():
                        errors.append(
                            f"execution.stages.{stage}.script does not exist: {script}"
                        )
                if not isinstance(function, str) or not SHELL_FUNCTION_PATTERN.fullmatch(function):
                    errors.append(
                        f"execution.stages.{stage}.function must be a valid Shell function name"
                    )
        timeout = execution.get("timeout_minutes")
        if not isinstance(timeout, int) or isinstance(timeout, bool) or timeout <= 0:
            errors.append("execution.timeout_minutes must be a positive integer")
        # 可选:该软件 build 模式所需的磁盘下限(GiB),缺省用框架默认值。
        min_free_disk = execution.get("min_free_disk_gb")
        if min_free_disk is not None and (
            not isinstance(min_free_disk, (int, float))
            or isinstance(min_free_disk, bool)
            or min_free_disk <= 0
        ):
            errors.append("execution.min_free_disk_gb must be a positive number")

    _validate_checks(case.get("checks"), errors)

    outputs = case.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        errors.append("outputs must be a non-empty mapping")
    else:
        output_paths: set[str] = set()
        for output_name, definition in outputs.items():
            if not isinstance(output_name, str) or not OUTPUT_NAME_PATTERN.fullmatch(output_name):
                errors.append("output names must use lowercase letters, numbers, and underscores")
                continue
            if not isinstance(definition, dict):
                errors.append(f"output {output_name} must be a mapping")
                continue
            unknown = set(definition) - {"path", "stage", "format", "required"}
            if unknown:
                errors.append(
                    f"output {output_name} contains unsupported fields: "
                    + ", ".join(sorted(str(field) for field in unknown))
                )
            missing = {
                field for field in ("path", "stage", "format", "required")
                if field not in definition
            }
            if missing:
                errors.append(
                    f"output {output_name} is missing fields: {', '.join(sorted(missing))}"
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
                elif output_path in output_paths:
                    errors.append(f"output path {output_path} is declared more than once")
                else:
                    output_paths.add(output_path)
            if definition.get("stage") not in FUNC_STAGES:
                errors.append(
                    f"output {output_name}.stage must be one of {list(FUNC_STAGES)}"
                )
            output_format = definition.get("format")
            if output_format not in VALID_OUTPUT_FORMATS:
                errors.append(
                    f"output {output_name}.format must be one of {sorted(VALID_OUTPUT_FORMATS)}"
                )
            elif (
                output_format == "json"
                and isinstance(output_path, str)
                and not output_path.endswith(".json")
            ):
                errors.append(f"output {output_name} with json format must use a .json path")
            if not isinstance(definition.get("required"), bool):
                errors.append(f"output {output_name}.required must be boolean")

    return case, errors


def validate_catalog(root: Path = ROOT) -> tuple[list[tuple[Path, dict[str, Any]]], list[str]]:
    entries: list[tuple[Path, dict[str, Any]]] = []
    errors: list[str] = []
    try:
        load_config(root)
    except (OSError, ValueError) as exc:
        return [], [f"{root / CONFIG_RELATIVE}: {exc}"]
    for path in discover_cases(root):
        case, case_errors = validate_case(path, root)
        if case is not None:
            entries.append((path, case))
        errors.extend(f"{path}: {error}" for error in case_errors)
    return entries, errors


def _parse_selection(value: str) -> set[str] | None:
    if value.strip() == "all":
        return None
    return {item.strip() for item in value.split(",") if item.strip()}


def build_matrix(
    software: str,
    version: str,
    operating_system: str,
    hardware: str,
    root: Path = ROOT,
) -> dict[str, Any]:
    config = load_config(root)
    entries, errors = validate_catalog(root)
    if errors:
        raise ValueError("invalid functional catalog: " + "; ".join(errors))

    selected_software = _parse_selection(software)
    selected_versions = _parse_selection(version)
    selected_os = _parse_selection(operating_system)
    selected_hardware = _parse_selection(hardware)

    unknown_os = (selected_os or set()) - set(config["operating_systems"])
    if unknown_os:
        raise ValueError(f"unknown operating systems: {', '.join(sorted(unknown_os))}")
    unknown_hardware = (selected_hardware or set()) - set(config["hardware"])
    if unknown_hardware:
        raise ValueError(f"unknown hardware: {', '.join(sorted(unknown_hardware))}")

    os_names = list(config["operating_systems"])
    hardware_names = list(config["hardware"])
    architecture = config["architectures"][0]

    include: list[dict[str, Any]] = []
    matched_software: set[str] = set()
    matched_versions: set[str] = set()

    for path, case in entries:
        if not case.get("enabled", False):
            continue
        case_name = case["name"]
        if selected_software is not None and case_name not in selected_software:
            continue
        matched_software.add(case_name)
        timeout = int(case["execution"].get("timeout_minutes", 90))
        for case_version in case["versions"]:
            if selected_versions is not None and case_version not in selected_versions:
                continue
            matched_versions.add(case_version)
            for os_name in os_names:
                if selected_os is not None and os_name not in selected_os:
                    continue
                os_spec = config["operating_systems"][os_name]
                if os_spec["runtime"] != "container":
                    image = ""
                elif os_spec["mode"] == "build":
                    image = os_spec["image"]
                else:
                    image = compose_image(
                        os_spec["image_template"],
                        case.get("image_name") or case_name,
                        case_version,
                        architecture,
                    )
                for hardware_name in hardware_names:
                    if selected_hardware is not None and hardware_name not in selected_hardware:
                        continue
                    hw_spec = config["hardware"][hardware_name]
                    include.append({
                        "category": case["category"],
                        "software": case_name,
                        "version": case_version,
                        "architecture": architecture,
                        "os": os_name,
                        "os_runtime": os_spec["runtime"],
                        "os_mode": os_spec["mode"],
                        "os_image_template": os_spec["image_template"],
                        "image": image,
                        "hardware": hardware_name,
                        "runner_labels": json.dumps(hw_spec["runner_labels"]),
                        "timeout_minutes": timeout,
                        "job_timeout_minutes": timeout + 30,
                        "case_path": path.relative_to(root).as_posix(),
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
    matrix_parser.add_argument("--os", default="all")
    matrix_parser.add_argument("--hardware", default="all")
    matrix_parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    if args.command == "validate":
        try:
            config = load_config(ROOT)
        except (OSError, ValueError) as exc:
            print(f"ERROR {ROOT / CONFIG_RELATIVE}: {exc}", file=sys.stderr)
            return 1
        entries, errors = validate_catalog(ROOT)
        if errors:
            for error in errors:
                print(f"ERROR {error}", file=sys.stderr)
            return 1
        print(
            "OK config: "
            f"{len(config['operating_systems'])} OS, "
            f"{len(config['hardware'])} hardware"
        )
        if not entries:
            print("OK no functional case manifests found; catalog is empty")
        for path, _case in entries:
            print(f"OK {path.relative_to(ROOT)}")
        return 0

    try:
        matrix = build_matrix(args.software, args.version, args.os, args.hardware)
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