#!/usr/bin/env python3
"""Collect local Envoy standalone-run metadata and render its report."""

from __future__ import annotations

import argparse
import json
import os
import platform
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def command(args: list[str]) -> str:
    try:
        completed = subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    except OSError:
        return "N/A"
    return completed.stdout.strip() if completed.returncode == 0 and completed.stdout.strip() else "N/A"


def system_info() -> dict[str, Any]:
    pretty_name = "ERROR"
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if line.startswith("PRETTY_NAME="):
                pretty_name = line.split("=", 1)[1].strip().strip('"')
                break
    except OSError:
        pass
    return {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "architecture": platform.machine(),
        "cpu_model": command(["lscpu", "-J"]),
        "cpu_count": os.cpu_count() or 0,
        "operating_system": pretty_name,
        "kernel": platform.release(),
        "python_version": platform.python_version(),
        "gcc_version": command(["gcc", "-dumpfullversion", "-dumpversion"]),
        "glibc_version": command(["getconf", "GNU_LIBC_VERSION"]),
        "numa": command(["numactl", "--hardware"]),
    }


def runtime_state() -> dict[str, Any]:
    return {"timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def render_report(result: dict[str, Any]) -> str:
    lines = [f"# Envoy {result['version']}", "", "## 性能指标", ""]
    groups: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for name, metric in result.get("metrics", {}).items():
        groups.setdefault(metric.get("group", "未分组"), []).append((name, metric))
    for group, metrics in groups.items():
        lines.extend([f"### {group}", "", "| 指标 | 数值 | 单位 | 优化方向 |", "|---|---:|---|---|"])
        for name, metric in metrics:
            direction = "越大越好" if metric["direction"] == "higher_is_better" else "越小越好"
            lines.append(f"| {name} | {metric['value']} | {metric['unit']} | {direction} |")
        lines.append("")
    return "\n".join(lines)


def finalize(output_dir: Path, version: str, architecture: str, run_id: str, command_status: str, cleanup_status: str, failed_stage: str) -> int:
    benchmark = load_json(output_dir / "benchmark_envoy.json")
    metrics = benchmark.get("results", {}) if command_status == "passed" else {}
    if command_status == "passed" and (not isinstance(metrics, dict) or not metrics):
        command_status = "failed"
        failed_stage = failed_stage or "test"
        metrics = {}
    result = {
        "software": "envoy",
        "version": version,
        "architecture": architecture,
        "run_id": run_id,
        "status": command_status,
        "cleanup_status": cleanup_status,
        "failed_stage": failed_stage or None,
        "build_info": load_json(output_dir / "build_info.json"),
        "system_info": load_json(output_dir / "system_info.json"),
        "runtime_before": load_json(output_dir / "runtime_before.json"),
        "runtime_after": load_json(output_dir / "runtime_after.json"),
        "parameters": benchmark.get("parameters", {}),
        "metrics": metrics,
    }
    write_json(output_dir / "results.json", result)
    write_json(output_dir / "status.json", {"status": command_status, "cleanup_status": cleanup_status, "failed_stage": failed_stage or None})
    (output_dir / "report.md").write_text(render_report(result), encoding="utf-8")
    return 0 if command_status == "passed" and cleanup_status == "passed" else 60


def main() -> int:
    parser = argparse.ArgumentParser()
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("system", "runtime"):
        actions.add_parser(action).add_argument("output", type=Path)
    build = actions.add_parser("build-info")
    build.add_argument("output", type=Path)
    build.add_argument("requested_version")
    build.add_argument("actual_version_file", type=Path)
    build.add_argument("architecture")
    build.add_argument("run_id")
    final = actions.add_parser("finalize")
    final.add_argument("output_dir", type=Path)
    final.add_argument("version")
    final.add_argument("architecture")
    final.add_argument("run_id")
    final.add_argument("command_status", choices=("passed", "failed"))
    final.add_argument("cleanup_status", choices=("passed", "failed"))
    final.add_argument("failed_stage", nargs="?", default="")
    args = parser.parse_args()
    if args.action == "system":
        write_json(args.output, system_info())
    elif args.action == "runtime":
        write_json(args.output, runtime_state())
    elif args.action == "build-info":
        actual = args.actual_version_file.read_text(encoding="utf-8").strip() if args.actual_version_file.exists() else "ERROR"
        write_json(args.output, {"requested_version": args.requested_version, "actual_version": actual, "architecture": args.architecture, "run_id": args.run_id, "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
    else:
        return finalize(args.output_dir, args.version, args.architecture, args.run_id, args.command_status, args.cleanup_status, args.failed_stage)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
