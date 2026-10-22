#!/usr/bin/env python3
"""Run one manually maintained software case."""

from __future__ import annotations

import argparse
import platform
import sys
from pathlib import Path

from build_info import BuildInfoError, record_build_info, reset_build_info
from catalog import ROOT, validate_case
from collect_environment import collect_runtime_state, collect_system_info
from command_adapter import run_command
from context import RunContext
from json_helper import atomic_write_json, load_json
from normalize_results import ResultValidationError, validate_stage_outputs, write_normalized
from reporting import render_single

STAGES = ("prepare", "build", "start", "test", "stop", "finalize")
STAGE_EXIT_CODES = {
    "build": 30,
    "start": 20,
    "test": 50,
    "stop": 50,
    "finalize": 60,
}


def normalize_arch(value: str) -> str:
    lowered = value.lower()
    if lowered in {"x86_64", "amd64"}:
        return "x86_64"
    if lowered in {"aarch64", "arm64"}:
        return "aarch64"
    return lowered


def update_status(context: RunContext, **updates: object) -> dict:
    path = context.output_dir / "status.json"
    payload = load_json(path, {})
    if not payload:
        payload = {
            "category": context.category,
            "software": context.software,
            "version": context.version,
            "architecture": context.architecture,
            "run_id": context.run_id,
            "status": "running",
            "stage": "initialized",
            "failed_stage": None,
            "exit_code": None,
            "cleanup_status": "pending",
        }
    payload.update(updates)
    atomic_write_json(path, payload)
    return payload


def build_context(args: argparse.Namespace, case: dict) -> RunContext:
    category = case["category"]
    software = case["name"]
    output_dir = (
        ROOT
        / ".perf-output"
        / category
        / software
        / args.version
        / args.architecture
        / args.run_id
    )
    work_dir = (
        Path("/home/runner/boostkit-perf")
        / software
        / args.version
        / args.architecture
        / args.run_id
    )
    return RunContext(
        root=ROOT,
        case_path=args.case.resolve(),
        case=case,
        category=category,
        software=software,
        version=args.version,
        architecture=args.architecture,
        run_id=args.run_id,
        output_dir=output_dir,
        work_dir=work_dir,
    )


def mark_failed(context: RunContext, stage: str, exit_code: int, **extra: object) -> None:
    current = load_json(context.output_dir / "status.json", {})
    update_status(
        context,
        status="failed",
        stage=stage,
        failed_stage=current.get("failed_stage") or stage,
        exit_code=current.get("exit_code") or exit_code,
        **extra,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--version", required=True)
    parser.add_argument("--architecture", required=True, choices=("x86_64", "aarch64"))
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--stage", required=True, choices=STAGES)
    args = parser.parse_args()

    case, errors = validate_case(args.case, ROOT)
    if errors or case is None:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 10
    if args.version not in case["versions"]:
        print(f"ERROR: version {args.version} is not declared", file=sys.stderr)
        return 10
    context = build_context(args, case)
    context.output_dir.mkdir(parents=True, exist_ok=True)
    context.work_dir.mkdir(parents=True, exist_ok=True)
    print(
        f"[stage] starting {args.stage}: {context.software} {context.version} "
        f"on {context.architecture}",
        flush=True,
    )

    if args.stage == "prepare":
        actual_arch = normalize_arch(platform.machine())
        if actual_arch != args.architecture:
            mark_failed(context, "prepare", 20, error=f"runner architecture is {actual_arch}")
            print(f"ERROR: expected {args.architecture}, runner is {actual_arch}", file=sys.stderr)
            return 20
        atomic_write_json(context.output_dir / "system_info.json", collect_system_info())
        atomic_write_json(context.output_dir / "runtime_before.json", collect_runtime_state())
        update_status(context, status="running", stage="prepare")
        print("[stage] prepare completed successfully", flush=True)
        print(context.output_dir)
        return 0

    if context.execution.get("type") != "shell-functions":
        mark_failed(context, args.stage, 10, error="shell-functions execution is required")
        return 10

    previous = load_json(context.output_dir / "status.json", {})
    update_status(context, stage=args.stage)

    if args.stage == "finalize":
        atomic_write_json(context.output_dir / "runtime_after.json", collect_runtime_state())
        command_status = "failed" if previous.get("status") == "failed" else "passed"
        try:
            normalized_path = write_normalized(context, command_status)
        except (ResultValidationError, OSError, ValueError) as exc:
            mark_failed(context, args.stage, STAGE_EXIT_CODES[args.stage], error=str(exc))
            print(f"ERROR: result validation failed: {exc}", file=sys.stderr)
            return STAGE_EXIT_CODES[args.stage]
        report = render_single(load_json(normalized_path, {}))
        (context.output_dir / "report.md").write_text(report, encoding="utf-8")
        if previous.get("status") == "failed":
            update_status(context, stage=args.stage)
            print("[stage] finalize completed; preserving the earlier failure", flush=True)
            return 0
        update_status(context, status="passed", stage="complete", exit_code=0)
        print("[stage] finalize completed successfully", flush=True)
        print(context.output_dir)
        return 0

    try:
        if args.stage == "build":
            reset_build_info(context)
        result = run_command(context, args.stage)
    except Exception as exc:
        exit_code = STAGE_EXIT_CODES[args.stage]
        mark_failed(context, args.stage, exit_code, error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return exit_code

    print(f"[stage] {args.stage} function exited with code {result.returncode}", flush=True)

    if result.returncode != 0:
        mark_failed(context, args.stage, result.returncode or STAGE_EXIT_CODES[args.stage])
        return result.returncode or STAGE_EXIT_CODES[args.stage]

    try:
        if args.stage == "build":
            record_build_info(context)
        validate_stage_outputs(context, args.stage)
    except (BuildInfoError, ResultValidationError, OSError, ValueError) as exc:
        exit_code = STAGE_EXIT_CODES[args.stage]
        mark_failed(context, args.stage, exit_code, error=str(exc))
        print(f"ERROR: stage output validation failed: {exc}", file=sys.stderr)
        return exit_code

    if args.stage == "stop":
        update_status(context, stage=args.stage, service_stop_status="passed")
        print("[stage] stop completed successfully", flush=True)
        return 0

    update_status(context, status="running", stage=args.stage)
    print(f"[stage] {args.stage} completed successfully", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
