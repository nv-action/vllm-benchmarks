#!/usr/bin/env python3
"""Collect standalone Sonic Go run metadata and render its local report."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def command(*args: str) -> str:
    try:
        completed = subprocess.run(args, text=True, capture_output=True, check=False, timeout=15)
    except OSError:
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def os_pretty_name() -> str:
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if key == "PRETTY_NAME" and separator:
                return " ".join(shlex.split(value)) or "ERROR"
    except (OSError, ValueError):
        pass
    return "ERROR"


def system_info() -> dict[str, Any]:
    lscpu = command("/usr/bin/env", "LC_ALL=C", "lscpu")
    fields = dict(
        line.split(":", 1) for line in lscpu.splitlines() if ":" in line
    )
    return {
        "timestamp": now(),
        "architecture": platform.machine(),
        "cpu_model": fields.get("Model name", "unknown").strip(),
        "cpu_cores": os.cpu_count(),
        "os": os_pretty_name(),
        "kernel": platform.release(),
        "python_version": platform.python_version(),
        "gcc_version": command("gcc", "-dumpfullversion", "-dumpversion") or "unknown",
        "glibc_version": command("getconf", "GNU_LIBC_VERSION") or "unknown",
        "numa_nodes": fields.get("NUMA node(s)", "N/A").strip(),
    }


def runtime_info() -> dict[str, Any]:
    go_binary = os.environ.get("SONIC_GO_BIN", "")
    return {"timestamp": now(), "go_version": command(go_binary, "version") if go_binary else "unknown"}


def markdown_table(rows: list[tuple[str, str]]) -> str:
    body = ["| 项目 | 值 |", "|---|---|"]
    body.extend(f"| {key} | {value} |" for key, value in rows)
    return "\n".join(body)


def finalize(output_dir: Path, version: str, architecture: str, status: int, failed_stage: str, cleanup: str) -> int:
    benchmark = read_json(output_dir / "benchmark_sonic_go.json")
    raw_results = benchmark.get("results")
    metrics: list[dict[str, Any]] = []
    if status == 0:
        if not isinstance(raw_results, dict) or not raw_results:
            raise RuntimeError("benchmark_sonic_go.json has no metrics")
        for result in raw_results.values():
            if not isinstance(result, dict):
                raise TypeError("benchmark result is invalid")
            for field in ("source_name", "value", "unit", "direction", "group"):
                if field not in result:
                    raise RuntimeError(f"benchmark result is missing {field}")
            metrics.append(result)
    actual_version = (output_dir / "actual-version.txt").read_text(encoding="utf-8").strip() if (output_dir / "actual-version.txt").exists() else "unknown"
    build = {
        "timestamp": now(),
        "software": "sonic-go",
        "requested_version": version,
        "software_version": actual_version,
        "architecture": architecture,
        "source": "https://github.com/bytedance/sonic.git",
        "source_tag": f"v{version}",
        "go_source": "https://gitcode.com/openeuler/golang.git",
        "go_version": os.environ.get("SONIC_GO_VERSION", "unknown"),
        "toolchain_target": os.environ.get("SONIC_GO_TOOLCHAIN", "unknown"),
    }
    write_json(output_dir / "build_info.json", build)
    write_json(output_dir / "results.json", {"software": "sonic-go", "version": version, "architecture": architecture, "metrics": metrics})
    write_json(output_dir / "status.json", {"status": "passed" if status == 0 else "failed", "failed_stage": failed_stage or None, "cleanup": cleanup})

    report = [f"# Sonic Go {version} ({architecture})", "", "## 构建信息", "", markdown_table([
        ("请求软件版本", version), ("实际软件版本", actual_version), ("构建信息记录时间", build["timestamp"]),
    ]), "", "## 系统信息", ""]
    info = read_json(output_dir / "system_info.json")
    report.append(markdown_table([(key, str(value)) for key, value in info.items()]))
    if metrics:
        for group in sorted({str(metric["group"]) for metric in metrics}):
            report.extend(["", f"## 性能指标：{group}", "", "| 指标 | 值 | 单位 | 方向 |", "|---|---:|---|---|"])
            report.extend(
                f"| {metric['source_name']} | {metric['value']} | {metric['unit']} | {metric['direction']} |"
                for metric in metrics if metric["group"] == group
            )
    elif status != 0:
        report.extend(["", f"测试失败阶段：{failed_stage or 'unknown'}"])
    (output_dir / "report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
    return status


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("system", "runtime"):
        sub = subparsers.add_parser(action)
        sub.add_argument("output", type=Path)
    final = subparsers.add_parser("finalize")
    final.add_argument("output_dir", type=Path)
    final.add_argument("version")
    final.add_argument("architecture")
    final.add_argument("status", type=int)
    final.add_argument("failed_stage")
    final.add_argument("cleanup")
    args = parser.parse_args()
    if args.action == "system":
        write_json(args.output, system_info())
        return 0
    if args.action == "runtime":
        write_json(args.output, runtime_info())
        return 0
    try:
        return finalize(args.output_dir, args.version, args.architecture, args.status, args.failed_stage, args.cleanup)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"[sonic-go] ERROR: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
