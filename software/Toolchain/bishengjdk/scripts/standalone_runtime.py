#!/usr/bin/env python3
"""Provide self-contained environment collection and reporting for BiSheng JDK."""

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
    jdk_version_string: str,
    source_url: str,
    source_sha256: str,
    source_repo: str,
    source_tag: str,
    boot_jdk_home: str,
    jmh_version: str,
    benchmark_name: str,
) -> None:
    actual_version = actual_version_file.read_text(encoding="utf-8").strip()
    if not actual_version:
        raise RuntimeError("actual BiSheng JDK version is empty")
    if not jdk_version_string:
        raise RuntimeError("source-built JDK version string is empty")
    atomic_write_json(
        output,
        {
            "recorded_at": timestamp(),
            "category": "Toolchain",
            "software": "bishengjdk",
            "requested_version": requested_version,
            "actual_version": actual_version,
            "architecture": architecture,
            "run_id": run_id,
            "jdk_version_string": jdk_version_string,
            "source": "BiSheng JDK 21 GA source archive",
            "source_url": source_url,
            "source_sha256": source_sha256,
            "source_repository": f"https://gitee.com/openeuler/{source_repo}",
            "source_tag": source_tag,
            "boot_jdk_home": boot_jdk_home,
            "benchmark_suite": "BiSheng JDK FloatingScalarVectorAbsDiff JMH",
            "jmh_version": jmh_version,
            "benchmark_name": benchmark_name,
            "build_command": (
                "sh make/devkit/createJMHBundle.sh; "
                "bash configure --with-debug-level=release --with-jmh=build/jmh/jars "
                "--with-jvm-variants=server --prefix=<isolated jdk> "
                "--disable-warnings-as-errors --disable-precompiled-headers; make images"
            ),
        },
    )


def extract_metrics(
    benchmark: dict[str, Any], version: str, architecture: str
) -> dict[str, Any]:
    if benchmark.get("software") != "bishengjdk":
        raise RuntimeError("benchmark_bishengjdk.json has an invalid software identity")
    if (
        benchmark.get("version") != version
        or benchmark.get("architecture") != architecture
    ):
        raise RuntimeError("benchmark_bishengjdk.json identity differs from this run")
    results = benchmark.get("results")
    if not isinstance(results, dict) or not results:
        raise RuntimeError("benchmark_bishengjdk.json is missing results")
    metrics: dict[str, Any] = {}
    for result_key, result in results.items():
        if not isinstance(result, dict):
            raise TypeError(f"bishengjdk benchmark {result_key} must be an object")
        metric_name = result.get("source_name")
        if not isinstance(metric_name, str) or not metric_name:
            raise RuntimeError(f"bishengjdk benchmark {result_key} has no source_name")
        if metric_name != result_key:
            raise RuntimeError(
                f"bishengjdk benchmark key {result_key} differs from source_name {metric_name}"
            )
        if metric_name in metrics:
            raise RuntimeError(f"duplicate bishengjdk benchmark: {metric_name}")
        if result.get("source_field") != "primaryMetric.score":
            raise RuntimeError(
                f"metric {metric_name} is not sourced from JMH primaryMetric.score"
            )
        if result.get("unit") != "ns/op":
            raise RuntimeError(f"metric {metric_name} is not expressed in ns/op")
        value = result.get("value")
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise TypeError(f"metric {metric_name} is missing or is not numeric")
        if not math.isfinite(float(value)) or value <= 0:
            raise RuntimeError(f"metric {metric_name} must be positive and finite")
        metrics[metric_name] = {
            "value": value,
            "unit": "ns/op",
            "direction": "lower_is_better",
        }
    if not metrics:
        raise RuntimeError("benchmark_bishengjdk.json contains no metrics")
    return metrics


def markdown_cell(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value).replace("|", "\\|").replace("\r\n", "<br>").replace("\n", "<br>")


def render_report(result: dict[str, Any]) -> str:
    lines = [
        f"# 毕昇 JDK {result['version']} 独立性能测试报告",
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
        ("JDK 版本", "jdk_version_string"),
        ("源码来源", "source"),
        ("源码下载地址", "source_url"),
        ("源码 SHA-256", "source_sha256"),
        ("源码仓库", "source_repository"),
        ("源码标签", "source_tag"),
        ("Boot JDK", "boot_jdk_home"),
        ("基准套件", "benchmark_suite"),
        ("JMH 版本", "jmh_version"),
        ("基准类", "benchmark_name"),
        ("构建方式", "build_command"),
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
        ("glibc 版本（系统）", "glibc_version"),
        ("NUMA", "numa"),
    ):
        lines.append(f"| {label} | {markdown_cell(system_info.get(field))} |")
    lines.extend(
        [
            "",
            "## 性能指标（JMH 平均耗时，单位为 ns/op）",
            "",
            "| 指标 | 数值 | 单位 | 优化方向 |",
            "|---|---:|---|---|",
        ]
    )
    for metric_name, metric in result.get("metrics", {}).items():
        lines.append(
            f"| {markdown_cell(metric_name)} | {metric['value']} | "
            f"{metric['unit']} | 越小越好 |"
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
    benchmark = load_json(output_dir / "benchmark_bishengjdk.json")
    error = ""
    metrics: dict[str, Any] = {}
    if command_status == "passed":
        try:
            if not benchmark:
                raise RuntimeError("benchmark_bishengjdk.json is missing or invalid")
            metrics = extract_metrics(benchmark, version, architecture)
        except (RuntimeError, TypeError) as exc:
            command_status = "failed"
            failed_stage = failed_stage or "test"
            error = str(exc)
    status = "passed" if command_status == cleanup_status == "passed" else "failed"
    result = {
        "software": "bishengjdk",
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
            "software": "bishengjdk",
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
    build.add_argument("jdk_version_string")
    build.add_argument(
        "--source-url",
        required=True,
        help="GitHub mirror download URL of the BiSheng JDK GA source archive",
    )
    build.add_argument(
        "--source-sha256",
        required=True,
        help="SHA-256 checksum of the BiSheng JDK GA source archive used for this run",
    )
    build.add_argument(
        "--source-repo",
        required=True,
        help="GitHub mirror repository providing the BiSheng JDK GA source archive",
    )
    build.add_argument(
        "--source-tag",
        required=True,
        help="GA source tag matching the source-built JDK release",
    )
    build.add_argument(
        "--boot-jdk-home",
        required=True,
        help="JDK used as the BiSheng JDK source-build boot JDK",
    )
    build.add_argument(
        "--jmh-version",
        required=True,
        help="JMH version used for the BiSheng JDK microbenchmark",
    )
    build.add_argument(
        "--benchmark",
        required=True,
        help="BiSheng JDK JMH benchmark class",
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
            args.jdk_version_string,
            args.source_url,
            args.source_sha256,
            args.source_repo,
            args.source_tag,
            args.boot_jdk_home,
            args.jmh_version,
            args.benchmark,
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
