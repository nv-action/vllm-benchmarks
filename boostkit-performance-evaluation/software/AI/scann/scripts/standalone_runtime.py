#!/usr/bin/env python3
"""Provide self-contained environment collection and reporting for ScaNN."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

METRIC_DEFINITIONS = (
    (
        "dot_product/build_time_s",
        "benchmark",
        "results.dot_product.build_time_s",
        "s",
        "lower_is_better",
    ),
    (
        "dot_product/qps",
        "benchmark",
        "results.dot_product.qps",
        "queries/s",
        "higher_is_better",
    ),
    (
        "dot_product/latency_per_query_us",
        "benchmark",
        "results.dot_product.latency_per_query_us",
        "us",
        "lower_is_better",
    ),
    (
        "dot_product/recall_at_k",
        "benchmark",
        "results.dot_product.recall_at_k",
        "ratio",
        "higher_is_better",
    ),
    (
        "squared_l2/build_time_s",
        "benchmark",
        "results.squared_l2.build_time_s",
        "s",
        "lower_is_better",
    ),
    (
        "squared_l2/qps",
        "benchmark",
        "results.squared_l2.qps",
        "queries/s",
        "higher_is_better",
    ),
    (
        "squared_l2/latency_per_query_us",
        "benchmark",
        "results.squared_l2.latency_per_query_us",
        "us",
        "lower_is_better",
    ),
    (
        "squared_l2/recall_at_k",
        "benchmark",
        "results.squared_l2.recall_at_k",
        "ratio",
        "higher_is_better",
    ),
)


def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def command(arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            arguments,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.strip()


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return payload


def nested_value(payload: dict[str, Any], dotted_path: str) -> Any:
    current: Any = payload
    for field in dotted_path.split("."):
        if not isinstance(current, dict) or field not in current:
            raise RuntimeError(f"required result path is missing: {dotted_path}")
        current = current[field]
    return current


def os_pretty_name() -> str:
    try:
        lines = (
            Path("/etc/os-release")
            .read_text(encoding="utf-8", errors="replace")
            .splitlines()
        )
    except OSError:
        return "ERROR"
    for line in lines:
        field, separator, value = line.partition("=")
        if separator and field.strip() == "PRETTY_NAME":
            try:
                parsed = shlex.split(value, posix=True)
            except ValueError:
                return "ERROR"
            return " ".join(parsed) if parsed else "ERROR"
    return "ERROR"


def cpu_model() -> str:
    lscpu_command = [
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/env",
        "LC_ALL=C",
        "/usr/bin/lscpu",
    ]
    for line in command(lscpu_command).splitlines():
        field, separator, value = line.partition(":")
        if separator and field.strip().casefold() == "model name" and value.strip():
            return value.strip()
    return "unknown"


def gcc_version() -> str:
    value = command(["gcc", "-dumpfullversion", "-dumpversion"])
    return value.splitlines()[0] if value else "unknown"


def glibc_version() -> str:
    value = command(["getconf", "GNU_LIBC_VERSION"])
    if value:
        return value.splitlines()[0]
    implementation, version = platform.libc_ver()
    if implementation and version:
        return f"{implementation} {version}"
    return "unknown"


def collect_system_info() -> dict[str, Any]:
    return {
        "collected_at": timestamp(),
        "architecture": platform.machine(),
        "platform": os_pretty_name(),
        "kernel": platform.release(),
        "cpu_model": cpu_model(),
        "cpu_count": os.cpu_count() or 0,
        "python_version": platform.python_version(),
        "gcc_version": gcc_version(),
        "glibc_version": glibc_version(),
        "numa": command(["numactl", "--hardware"]),
    }


def collect_runtime_state() -> dict[str, Any]:
    governor = command(
        [
            "sh",
            "-c",
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null",
        ]
    )
    return {
        "collected_at": timestamp(),
        "memory": command(["free", "-b"]),
        "cpu_governor": governor,
    }


def record_build_info(
    output: Path,
    requested_version: str,
    actual_version_file: Path,
    architecture: str,
    run_id: str,
) -> None:
    actual_version = actual_version_file.read_text(encoding="utf-8").strip()
    if not actual_version:
        raise RuntimeError("actual ScaNN version is empty")
    atomic_write_json(
        output,
        {
            "recorded_at": timestamp(),
            "category": "AI",
            "software": "scann",
            "requested_version": requested_version,
            "actual_version": actual_version,
            "architecture": architecture,
            "run_id": run_id,
        },
    )


def validate_identity(
    payload: dict[str, Any],
    filename: str,
    version: str,
    architecture: str,
) -> None:
    if payload.get("software") != "scann":
        raise RuntimeError(f"{filename} has an invalid software identity")
    if payload.get("version") != version:
        raise RuntimeError(f"{filename} has an unexpected ScaNN version")
    if payload.get("architecture") != architecture:
        raise RuntimeError(f"{filename} has an unexpected architecture")


def extract_metrics(
    benchmark: dict[str, Any],
    version: str,
    architecture: str,
) -> dict[str, Any]:
    validate_identity(benchmark, "benchmark.json", version, architecture)
    metrics: dict[str, Any] = {}
    for metric_name, _source_name, path, unit, direction in METRIC_DEFINITIONS:
        value = nested_value(benchmark, path)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"metric {metric_name} must be numeric")
        if not math.isfinite(float(value)):
            raise RuntimeError(f"metric {metric_name} must be finite")
        metrics[metric_name] = {
            "value": value,
            "unit": unit,
            "direction": direction,
        }
    return metrics


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def direction_label(direction: str) -> str:
    labels = {
        "higher_is_better": "越大越好",
        "lower_is_better": "越小越好",
        "neutral": "仅供参考",
    }
    return labels[direction]


def render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# ScaNN {result['version']} 独立性能测试报告",
        "",
        f"- Run ID：`{result['run_id']}`",
        f"- 架构：`{result['architecture']}`",
        f"- 状态：`{result['status']}`",
        f"- 清理状态：`{result['cleanup_status']}`",
        "",
        "## 构建信息",
        "",
        "| 项目 | 值 |",
        "|---|---|",
    ]
    build_info = result.get("build_info", {})
    for label, field in (
        ("请求软件版本", "requested_version"),
        ("实际软件版本", "actual_version"),
        ("记录时间", "recorded_at"),
    ):
        lines.append(f"| {label} | {markdown_cell(build_info.get(field))} |")
    lines.extend(["", "## 系统信息", "", "| 项目 | 值 |", "|---|---|"])
    system_info = result.get("system_info", {})
    for label, field in (
        ("操作系统", "platform"),
        ("系统架构", "architecture"),
        ("CPU 型号", "cpu_model"),
        ("CPU 核数", "cpu_count"),
        ("内核", "kernel"),
        ("Python 版本", "python_version"),
        ("GCC 版本", "gcc_version"),
        ("glibc 版本", "glibc_version"),
        ("NUMA", "numa"),
    ):
        lines.append(f"| {label} | {markdown_cell(system_info.get(field))} |")
    lines.extend(
        [
            "",
            "## 性能指标",
            "",
            "| 指标 | 数值 | 单位 | 优化方向 |",
            "|---|---:|---|---|",
        ]
    )
    for metric_name, metric in result.get("metrics", {}).items():
        lines.append(
            f"| {markdown_cell(metric_name)} | {metric['value']} | "
            f"{metric['unit']} | {direction_label(metric['direction'])} |"
        )
    if result.get("error"):
        lines.extend(["", "## 错误", "", markdown_cell(result["error"])])
    lines.append("")
    return "\n".join(lines)


def finalize(
    output_dir: Path,
    version: str,
    architecture: str,
    run_id: str,
    command_status: str,
    cleanup_status: str,
    failed_stage: str | None,
) -> int:
    benchmark = load_json(output_dir / "benchmark.json")
    error = ""
    metrics: dict[str, Any] = {}
    if command_status == "passed":
        try:
            if not benchmark:
                raise RuntimeError("benchmark.json is missing or invalid")
            metrics = extract_metrics(benchmark, version, architecture)
        except (RuntimeError, TypeError) as exception:
            command_status = "failed"
            failed_stage = failed_stage or "test"
            error = str(exception)
    status = "passed" if command_status == cleanup_status == "passed" else "failed"
    result = {
        "software": "scann",
        "category": "AI",
        "version": version,
        "architecture": architecture,
        "run_id": run_id,
        "status": status,
        "cleanup_status": cleanup_status,
        "failed_stage": failed_stage,
        "build_info": load_json(output_dir / "build_info.json"),
        "system_info": load_json(output_dir / "system_info.json"),
        "runtime_before": load_json(output_dir / "runtime_before.json"),
        "runtime_after": load_json(output_dir / "runtime_after.json"),
        "parameters": {
            "benchmark.json": benchmark.get("parameters", {}),
        },
        "metrics": metrics,
        "error": error or None,
    }
    atomic_write_json(output_dir / "results.json", result)
    atomic_write_json(
        output_dir / "status.json",
        {
            "software": "scann",
            "category": "AI",
            "version": version,
            "architecture": architecture,
            "run_id": run_id,
            "status": status,
            "failed_stage": failed_stage,
            "cleanup_status": cleanup_status,
            "error": error or None,
        },
    )
    (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    return 60 if error else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for command_name in ("system", "runtime"):
        command_parser = subparsers.add_parser(command_name)
        command_parser.add_argument("output", type=Path)
    build_parser = subparsers.add_parser("build-info")
    build_parser.add_argument("output", type=Path)
    build_parser.add_argument("requested_version")
    build_parser.add_argument("actual_version_file", type=Path)
    build_parser.add_argument("architecture")
    build_parser.add_argument("run_id")
    finalize_parser = subparsers.add_parser("finalize")
    finalize_parser.add_argument("output_dir", type=Path)
    finalize_parser.add_argument("version")
    finalize_parser.add_argument("architecture")
    finalize_parser.add_argument("run_id")
    finalize_parser.add_argument("command_status", choices=("passed", "failed"))
    finalize_parser.add_argument("cleanup_status", choices=("passed", "failed"))
    finalize_parser.add_argument("failed_stage", nargs="?", default="")
    return parser.parse_args()


def main() -> int:
    arguments = parse_args()
    if arguments.command == "system":
        atomic_write_json(arguments.output, collect_system_info())
        return 0
    if arguments.command == "runtime":
        atomic_write_json(arguments.output, collect_runtime_state())
        return 0
    if arguments.command == "build-info":
        record_build_info(
            arguments.output,
            arguments.requested_version,
            arguments.actual_version_file,
            arguments.architecture,
            arguments.run_id,
        )
        return 0
    return finalize(
        arguments.output_dir,
        arguments.version,
        arguments.architecture,
        arguments.run_id,
        arguments.command_status,
        arguments.cleanup_status,
        arguments.failed_stage or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
