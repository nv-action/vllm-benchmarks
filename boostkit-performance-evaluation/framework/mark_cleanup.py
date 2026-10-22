#!/usr/bin/env python3
"""Persist post-cleanup outcome into task status and normalized result files."""

from __future__ import annotations

import argparse
from pathlib import Path

from json_helper import atomic_write_json, load_json


def mark(output_dir: Path, outcome: str) -> None:
    cleanup_status = "passed" if outcome == "success" else "failed"
    status_path = output_dir / "status.json"
    normalized_path = output_dir / "normalized_result.json"

    status = load_json(status_path, {})
    if status:
        status["cleanup_status"] = cleanup_status
        if cleanup_status == "failed":
            status.update({"status": "failed", "stage": "cleanup", "failed_stage": "cleanup", "exit_code": 80})
        atomic_write_json(status_path, status)

    normalized = load_json(normalized_path, {})
    if normalized:
        normalized["cleanup_status"] = cleanup_status
        if cleanup_status == "failed":
            normalized.update({"status": "failed", "failed_stage": "cleanup"})
        atomic_write_json(normalized_path, normalized)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--outcome", required=True)
    args = parser.parse_args()
    mark(args.output_dir, args.outcome)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
