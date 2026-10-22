#!/usr/bin/env python3
"""Aggregate functional cell results into a PASS/FAIL matrix report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any
from xml.etree.ElementTree import Element, SubElement, tostring

STATUS_LABELS = {"passed": "PASS", "failed": "FAIL", "unknown": "?"}


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def collect_results(input_root: Path) -> list[dict[str, Any]]:
    """Collect one entry per cell, preferring result.json over status.json."""
    results: dict[Path, dict[str, Any]] = {}
    for path in sorted(input_root.rglob("status.json")):
        results[path.parent.resolve()] = {
            "software": None, "status": "unknown", "checks": [],
        }
    for path in sorted(input_root.rglob("result.json")):
        payload = load_json(path)
        if payload:
            results[path.parent.resolve()] = payload
    collected: list[dict[str, Any]] = []
    for directory, payload in results.items():
        if not payload.get("software"):
            status = load_json(directory / "status.json")
            payload = {**payload, **status}
        if payload.get("software"):
            collected.append(payload)
    return collected


def build_matrix(results: list[dict[str, Any]]) -> dict[str, Any]:
    """精简结构：软件 -> 版本 -> OS -> 硬件 -> 状态。

    例：{"redis": {"6.0.20": {"openeuler-24.03-sp4": {"kunpeng920": "passed",
                                                     "kunpeng950": "failed"}}}}
    """
    matrix: dict[str, dict[str, dict[str, dict[str, str]]]] = {}
    for result in sorted(
        results,
        key=lambda item: (
            str(item.get("software", "?")),
            str(item.get("version", "?")),
            str(item.get("os", "?")),
            str(item.get("hardware", "?")),
        ),
    ):
        software = str(result.get("software", "?"))
        version = str(result.get("version", "?"))
        os_name = str(result.get("os", "?"))
        hardware = str(result.get("hardware", "?"))
        matrix.setdefault(software, {}).setdefault(version, {}).setdefault(os_name, {})[
            hardware
        ] = result.get("status", "unknown")
    return matrix


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "total_cells": len(results),
        "passed": sum(1 for r in results if r.get("status") == "passed"),
        "failed": sum(1 for r in results if r.get("status") != "passed"),
    }


def render_markdown(matrix: dict[str, Any], results: list[dict[str, Any]]) -> str:
    rows = [(software, version) for software, versions in matrix.items() for version in versions]
    os_names = sorted({os_name for versions in matrix.values() for ver in versions.values() for os_name in ver})
    summary = summarize(results)
    lines = [
        "# 功能测试矩阵",
        "",
        f"- 单元格总数：{summary['total_cells']}",
        f"- 通过：{summary['passed']}",
        f"- 失败：{summary['failed']}",
    ]

    for os_name in os_names:
        # 该 OS 下出现过的硬件（列），以及有结果的行（软件 × 版本）。
        hardware_names = sorted({
            hardware
            for versions in matrix.values()
            for ver in versions.values()
            for entry_os, hw_map in ver.items()
            if entry_os == os_name
            for hardware in hw_map
        })
        lines.extend(["", f"## {os_name}", ""])
        header = ["软件", "版本", *hardware_names]
        lines.append("| " + " | ".join(header) + " |")
        lines.append("|" + "---|" * len(header))
        for software, version in sorted(rows):
            hw_map = matrix.get(software, {}).get(version, {}).get(os_name)
            if hw_map is None:
                continue
            rendered = [
                STATUS_LABELS.get(hw_map.get(hardware, "unknown"), STATUS_LABELS["unknown"])
                for hardware in hardware_names
            ]
            lines.append("| " + " | ".join([software, version, *rendered]) + " |")

    detail = [
        result for result in results
        if result.get("status") != "passed" and result.get("software")
    ]
    if detail:
        lines.extend(["", "## 未通过明细", ""])
        lines.append("| OS | 硬件 | 软件 | 版本 | 检查项 |")
        lines.append("|---|---|---|---|---|")
        for result in sorted(
            detail,
            key=lambda item: (
                str(item.get("os", "?")), str(item.get("hardware", "?")),
                str(item.get("software", "?")), str(item.get("version", "?")),
            ),
        ):
            failing = [
                check for check in result.get("checks", [])
                if isinstance(check, dict) and check.get("status") != "passed"
            ]
            if failing:
                for check in failing:
                    lines.append(
                        f"| {result.get('os')} | {result.get('hardware')} | "
                        f"{result.get('software')} | {result.get('version')} | "
                        f"{check.get('name')}: {check.get('detail', '')} |"
                    )
            else:
                lines.append(
                    f"| {result.get('os')} | {result.get('hardware')} | "
                    f"{result.get('software')} | {result.get('version')} | "
                    f"{result.get('status')} |"
                )
    lines.append("")
    return "\n".join(lines)


def render_junit(results: list[dict[str, Any]]) -> str:
    failures = sum(1 for r in results if r.get("status") != "passed")
    suite = Element(
        "testsuite", name="functional", tests=str(len(results)), failures=str(failures)
    )
    for result in results:
        case = SubElement(
            suite,
            "testcase",
            classname=f"{result.get('category')}.{result.get('software')}",
            name=f"{result.get('version')}.{result.get('os')}.{result.get('hardware')}",
        )
        if result.get("status") != "passed":
            failure = SubElement(case, "failure", message="functional test failed")
            failure.text = str(result.get("status"))
    return tostring(suite, encoding="unicode") + "\n"


def generate(input_root: Path, output_dir: Path) -> dict[str, Any]:
    results = collect_results(input_root)
    matrix = build_matrix(results)
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "combined-report.json", matrix)
    (output_dir / "combined-report.md").write_text(
        render_markdown(matrix, results), encoding="utf-8"
    )
    (output_dir / "junit.xml").write_text(render_junit(results), encoding="utf-8")
    return summarize(results)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-root", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    summary = generate(args.input_root, args.output_dir)
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())