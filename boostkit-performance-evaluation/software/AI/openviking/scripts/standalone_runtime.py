#!/usr/bin/env python3
"""Provide self-contained environment collection and reporting for OpenViking."""

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

def timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def direction_label(direction: str) -> str:
    return {
        "higher_is_better": "越大越好",
        "lower_is_better": "越小越好",
        "neutral": "仅展示",
    }[direction]

def command(args: list[str]) -> str:
    try:
        completed = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def os_pretty_name() -> str:
    try:
        lines = Path("/etc/os-release").read_text(
            encoding="utf-8", errors="replace"
        ).splitlines()
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
        if separator and field.strip().casefold() == "model name":
            if value.strip():
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
    return f"{implementation} {version}" if implementation and version else "unknown"


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
    governor = command([
        "sh",
        "-c",
        "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null",
    ])
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
        raise RuntimeError("actual OpenViking version is empty")
    atomic_write_json(output, {
        "recorded_at": timestamp(),
        "category": "AI",
        "software": "openviking",
        "requested_version": requested_version,
        "actual_version": actual_version,
        "architecture": architecture,
        "run_id": run_id,
    })


def extract_metrics(
    aggregate: dict[str, Any], version: str, architecture: str
) -> dict[str, Any]:
    if aggregate.get("software") != "openviking":
        raise RuntimeError("results.json has an invalid software identity")
    if aggregate.get("version") != version or aggregate.get("architecture") != architecture:
        raise RuntimeError("results.json identity differs from this run")
    results = aggregate.get("results")
    if not isinstance(results, dict) or not results:
        raise RuntimeError("results.json is missing source metrics")
    metrics: dict[str, Any] = {}
    for metric_name, metric in results.items():
        if not isinstance(metric_name, str) or not isinstance(metric, dict):
            raise RuntimeError("results.json has an invalid metric entry")
        source_name = metric.get("source_name")
        if source_name != metric_name:
            raise RuntimeError(f"metric source name differs from its result key: {metric_name}")
        value = metric.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"metric {metric_name} is not numeric")
        if not math.isfinite(float(value)) or value <= 0:
            raise RuntimeError(f"metric {metric_name} must be positive and finite")
        unit = metric.get("unit")
        direction = metric.get("direction")
        if not isinstance(unit, str) or not unit:
            raise RuntimeError(f"metric {metric_name} has no unit")
        if direction not in {"higher_is_better", "lower_is_better", "neutral"}:
            raise RuntimeError(f"metric {metric_name} has an invalid direction")
        metrics[metric_name] = {
            "value": value,
            "unit": unit,
            "direction": direction,
        }
    if not metrics:
        raise RuntimeError("results.json contains no metrics")
    return metrics


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# OpenViking {result['version']} 独立性能测试报告",
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
    lines.extend([
        "",
        "## 性能指标",
        "",
        "| 指标 | 数值 | 单位 | 优化方向 |",
        "|---|---:|---|---|",
    ])
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
    aggregate = load_json(output_dir / "results.json")
    error = ""
    metrics: dict[str, Any] = {}
    if command_status == "passed":
        try:
            if not aggregate:
                raise RuntimeError("results.json is missing or invalid")
            metrics = extract_metrics(aggregate, version, architecture)
        except RuntimeError as exc:
            command_status = "failed"
            failed_stage = failed_stage or "test"
            error = str(exc)
    status = "passed" if command_status == cleanup_status == "passed" else "failed"
    result = {
        "software": "openviking",
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
        "metrics": metrics,
        "error": error or None,
    }
    atomic_write_json(output_dir / "results.json", result)
    atomic_write_json(output_dir / "status.json", {
        "software": "openviking",
        "category": "AI",
        "version": version,
        "architecture": architecture,
        "run_id": run_id,
        "status": status,
        "failed_stage": failed_stage,
        "cleanup_status": cleanup_status,
        "error": error or None,
    })
    (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    return 60 if error else 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("system", "runtime"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("output", type=Path)
    build = subparsers.add_parser("build-info")
    build.add_argument("output", type=Path)
    build.add_argument("requested_version")
    build.add_argument("actual_version_file", type=Path)
    build.add_argument("architecture")
    build.add_argument("run_id")
    final = subparsers.add_parser("finalize")
    final.add_argument("output_dir", type=Path)
    final.add_argument("version")
    final.add_argument("architecture")
    final.add_argument("run_id")
    final.add_argument("command_status", choices=("passed", "failed"))
    final.add_argument("cleanup_status", choices=("passed", "failed"))
    final.add_argument("failed_stage", nargs="?", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.command == "system":
        atomic_write_json(args.output, collect_system_info())
        return 0
    if args.command == "runtime":
        atomic_write_json(args.output, collect_runtime_state())
        return 0
    if args.command == "build-info":
        record_build_info(
            args.output,
            args.requested_version,
            args.actual_version_file,
            args.architecture,
            args.run_id,
        )
        return 0
    return finalize(
        args.output_dir,
        args.version,
        args.architecture,
        args.run_id,
        args.command_status,
        args.cleanup_status,
        args.failed_stage or None,
    )


if __name__ == "__main__":
    raise SystemExit(main())
