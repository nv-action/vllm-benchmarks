#!/usr/bin/env python3
"""Collect standalone Flink evidence and render a local report."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def command(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, capture_output=True, text=True, check=False, timeout=10)
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def os_pretty_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                return line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    return "ERROR"


def system_info() -> dict[str, Any]:
    cpu_model = "unknown"
    for line in command(["lscpu"]).splitlines():
        field, separator, value = line.partition(":")
        if separator and field.strip().casefold() == "model name" and value.strip():
            cpu_model = value.strip()
            break
    return {
        "collected_at": now(),
        "architecture": platform.machine(),
        "platform": os_pretty_name(),
        "kernel": platform.release(),
        "cpu_model": cpu_model,
        "cpu_count": os.cpu_count() or 0,
        "python_version": platform.python_version(),
        "gcc_version": command(["gcc", "-dumpfullversion", "-dumpversion"]) or "unknown",
        "glibc_version": command(["getconf", "GNU_LIBC_VERSION"]) or "unknown",
        "numa": command(["numactl", "--hardware"]) or "N/A",
    }


def runtime_info() -> dict[str, Any]:
    return {"collected_at": now(), "memory": command(["free", "-b"])}


def build_info(output: Path, requested: str, actual_file: Path, architecture: str, run_id: str) -> None:
    actual = actual_file.read_text(encoding="utf-8").strip()
    if not actual:
        raise RuntimeError("actual Flink version is empty")
    write_json(output, {
        "recorded_at": now(),
        "category": "Bigdata",
        "software": "flink",
        "requested_version": requested,
        "actual_version": actual,
        "architecture": architecture,
        "run_id": run_id,
    })


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\n", "<br>")


def render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# Flink {result['version']} 独立性能测试报告",
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
    for label, field in (("请求软件版本", "requested_version"), ("实际软件版本", "actual_version"), ("记录时间", "recorded_at")):
        lines.append(f"| {label} | {markdown_cell(result.get('build_info', {}).get(field))} |")
    lines.extend(["", "## 性能指标", "", "| 指标 | 数值 | 单位 | 优化方向 |", "|---|---:|---|---|"])
    for metric in result.get("metrics", {}).values():
        direction = "越大越好" if metric.get("direction") == "higher_is_better" else metric.get("direction")
        lines.append(f"| {markdown_cell(metric.get('source_name'))} | {markdown_cell(metric.get('value'))} | {markdown_cell(metric.get('unit'))} | {markdown_cell(direction)} |")
    if result.get("error"):
        lines.extend(["", "## 错误", "", markdown_cell(result["error"])])
    lines.append("")
    return "\n".join(lines)


def finalize(output_dir: Path, version: str, architecture: str, run_id: str, stage_status: int, cleanup_status: str, failed_stage: str) -> int:
    benchmark = load_json(output_dir / "benchmark_flink.json")
    error = ""
    metrics: dict[str, Any] = {}
    if stage_status == 0:
        results = benchmark.get("results")
        if not isinstance(results, dict) or len(results) != 1:
            error = "benchmark_flink.json must contain exactly one result"
        else:
            metrics = results
    if error:
        stage_status = 50
        failed_stage = failed_stage or "test"
    status = "passed" if stage_status == 0 and cleanup_status == "passed" else "failed"
    result = {
        "software": "flink",
        "category": "Bigdata",
        "version": version,
        "architecture": architecture,
        "run_id": run_id,
        "status": status,
        "cleanup_status": cleanup_status,
        "failed_stage": failed_stage or None,
        "build_info": load_json(output_dir / "build_info.json"),
        "system_info": load_json(output_dir / "system_info.json"),
        "runtime_before": load_json(output_dir / "runtime_before.json"),
        "runtime_after": load_json(output_dir / "runtime_after.json"),
        "parameters": benchmark.get("parameters", {}),
        "metrics": metrics,
        "error": error or None,
    }
    write_json(output_dir / "results.json", result)
    write_json(output_dir / "status.json", {
        "software": "flink",
        "category": "Bigdata",
        "version": version,
        "architecture": architecture,
        "run_id": run_id,
        "status": status,
        "stage": "complete" if status == "passed" else failed_stage or "failed",
        "failed_stage": failed_stage or None,
        "exit_code": stage_status,
        "cleanup_status": cleanup_status,
    })
    (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    return stage_status


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("system", "runtime", "build-info", "finalize"))
    parser.add_argument("arguments", nargs="+")
    args = parser.parse_args()
    if args.action == "system":
        write_json(Path(args.arguments[0]), system_info())
    elif args.action == "runtime":
        write_json(Path(args.arguments[0]), runtime_info())
    elif args.action == "build-info":
        output, requested, actual, architecture, run_id = args.arguments
        build_info(Path(output), requested, Path(actual), architecture, run_id)
    else:
        output, version, architecture, run_id, stage_status, cleanup_status, failed_stage = args.arguments
        return finalize(Path(output), version, architecture, run_id, int(stage_status), cleanup_status, failed_stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
