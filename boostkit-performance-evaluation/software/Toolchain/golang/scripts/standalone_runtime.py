#!/usr/bin/env python3
"""Provide self-contained environment collection and reporting for golang."""

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
    gcc_version: str,
    release_url: str,
) -> None:
    actual_version = actual_version_file.read_text(encoding="utf-8").strip()
    if not actual_version:
        raise RuntimeError("actual Go version is empty")
    atomic_write_json(
        output,
        {
            "recorded_at": timestamp(),
            "category": "Toolchain",
            "software": "golang",
            "requested_version": requested_version,
            "actual_version": actual_version,
            "architecture": architecture,
            "run_id": run_id,
            "gcc_version": gcc_version,
            "release_url": release_url,
            "installation_method": "official precompiled binary distribution",
        },
    )


def extract_metrics(
    benchmark: dict[str, Any], version: str, architecture: str
) -> dict[str, Any]:
    if benchmark.get("software") != "golang":
        raise RuntimeError("benchmark_golang.json has an invalid software identity")
    if (
        benchmark.get("version") != version
        or benchmark.get("architecture") != architecture
    ):
        raise RuntimeError("benchmark_golang.json identity differs from this run")
    results = benchmark.get("results")
    if not isinstance(results, dict) or not results:
        raise RuntimeError("benchmark_golang.json is missing results")
    metrics: dict[str, Any] = {}
    for result_key, result in results.items():
        if not isinstance(result, dict):
            raise TypeError(f"golang benchmark {result_key} must be an object")
        metric_name = result.get("source_name")
        if not isinstance(metric_name, str) or not metric_name:
            raise RuntimeError(f"golang benchmark {result_key} has no source_name")
        if metric_name != result_key:
            raise RuntimeError(
                f"golang benchmark key {result_key} differs from source_name {metric_name}"
            )
        if metric_name in metrics:
            raise RuntimeError(f"duplicate golang benchmark: {metric_name}")
        source_benchmark = result.get("source_benchmark")
        source_package = result.get("source_package")
        source_field = result.get("source_field")
        unit = result.get("unit")
        direction = result.get("direction")
        group = result.get("group")
        if not all(
            isinstance(field, str) and field
            for field in (source_benchmark, source_package, source_field, unit, group)
        ):
            raise RuntimeError(
                f"metric {metric_name} must preserve its official source fields"
            )
        if source_field != unit:
            raise RuntimeError(f"metric {metric_name} changes its original unit")
        if direction not in {"higher_is_better", "lower_is_better"}:
            raise RuntimeError(f"metric {metric_name} has an invalid optimization direction")
        value = result.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"metric {metric_name} is missing or is not numeric")
        if not math.isfinite(float(value)) or value < 0:
            raise RuntimeError(f"metric {metric_name} must be non-negative and finite")
        metrics[metric_name] = {
            "value": value,
            "unit": unit,
            "direction": direction,
            "group": group,
        }
    if not metrics:
        raise RuntimeError("benchmark_golang.json contains no metrics")
    return metrics


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# golang {result['version']} 独立性能测试报告",
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
        ("官方发布地址", "release_url"),
        ("安装方式", "installation_method"),
        ("构建编译器 (gcc)", "gcc_version"),
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
    lines.extend(["", "## 性能指标（官方 cmd/bench 原始字段，median）"])
    metrics_by_group: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for metric_name, metric in result.get("metrics", {}).items():
        group = str(metric.get("group", "未分组"))
        metrics_by_group.setdefault(group, []).append((metric_name, metric))
    for group in sorted(metrics_by_group):
        lines.extend(
            [
                "",
                f"### {markdown_cell(group)}",
                "",
                "| 指标 | 数值 | 单位 | 优化方向 |",
                "|---|---:|---|---|",
            ]
        )
        for metric_name, metric in sorted(metrics_by_group[group]):
            direction = "越大越好"
            if metric["direction"] == "lower_is_better":
                direction = "越小越好"
            lines.append(
                f"| {markdown_cell(metric_name)} | {metric['value']} | "
                f"{metric['unit']} | {direction} |"
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
    benchmark = load_json(output_dir / "benchmark_golang.json")
    error = ""
    metrics: dict[str, Any] = {}
    if command_status == "passed":
        try:
            if not benchmark:
                raise RuntimeError("benchmark_golang.json is missing or invalid")
            metrics = extract_metrics(benchmark, version, architecture)
        except (RuntimeError, TypeError) as exc:
            command_status = "failed"
            failed_stage = failed_stage or "test"
            error = str(exc)
    status = "passed" if command_status == cleanup_status == "passed" else "failed"
    result = {
        "software": "golang",
        "category": "Toolchain",
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
        "parameters": benchmark.get("parameters", {}),
        "metrics": metrics,
        "error": error or None,
    }
    atomic_write_json(output_dir / "results.json", result)
    atomic_write_json(
        output_dir / "status.json",
        {
            "software": "golang",
            "category": "Toolchain",
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
    for name in ("system", "runtime"):
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("output", type=Path)
    build = subparsers.add_parser("build-info")
    build.add_argument("output", type=Path)
    build.add_argument("requested_version")
    build.add_argument("actual_version_file", type=Path)
    build.add_argument("architecture")
    build.add_argument("run_id")
    build.add_argument("gcc_version")
    build.add_argument(
        "--release-url",
        required=True,
        help="official release location of the Go binary distribution",
    )
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
            args.gcc_version,
            args.release_url,
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
