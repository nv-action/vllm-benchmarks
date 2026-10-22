#!/usr/bin/env python3
"""Validate software-reported versions and write common build identity."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from context import RunContext
from json_helper import atomic_write_json, load_json


ACTUAL_VERSION_FILENAME = "actual-version.txt"
BUILD_INFO_FILENAME = "build_info.json"


class BuildInfoError(ValueError):
    """Raised when a build does not report the requested software version."""


def actual_version_path(context: RunContext) -> Path:
    return context.work_dir / ACTUAL_VERSION_FILENAME


def build_info_path(context: RunContext) -> Path:
    return context.output_dir / BUILD_INFO_FILENAME


def reset_build_info(context: RunContext) -> None:
    """Remove build identity from an earlier attempt before invoking build."""
    actual_version_path(context).unlink(missing_ok=True)
    build_info_path(context).unlink(missing_ok=True)


def _read_actual_version(context: RunContext) -> str:
    path = actual_version_path(context)
    if not path.is_file():
        raise BuildInfoError(
            "build did not report its actual version through PERF_ACTUAL_VERSION_FILE"
        )
    if path.stat().st_size == 0:
        raise BuildInfoError("reported actual version is empty")
    if path.stat().st_size > 1024:
        raise BuildInfoError("reported actual version exceeds 1024 bytes")
    try:
        raw = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise BuildInfoError("reported actual version is not UTF-8") from exc
    lines = raw.splitlines()
    if len(lines) != 1 or not lines[0] or lines[0] != lines[0].strip():
        raise BuildInfoError("reported actual version must be exactly one trimmed line")
    return lines[0]


def record_build_info(context: RunContext) -> Path:
    """Validate the software value and create Framework-owned build_info.json."""
    actual_version = _read_actual_version(context)
    if actual_version != context.version:
        raise BuildInfoError(
            f"requested version {context.version}, built version {actual_version}"
        )
    output = build_info_path(context)
    atomic_write_json(
        output,
        {
            "recorded_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "category": context.category,
            "software": context.software,
            "requested_version": context.version,
            "actual_version": actual_version,
            "architecture": context.architecture,
            "run_id": context.run_id,
        },
    )
    return output


def load_build_info(context: RunContext) -> dict[str, Any]:
    """Load and revalidate Framework-owned build identity during finalize."""
    path = build_info_path(context)
    try:
        payload = load_json(path)
    except (OSError, ValueError) as exc:
        raise BuildInfoError(f"invalid {BUILD_INFO_FILENAME}: {exc}") from exc
    if not isinstance(payload, dict):
        raise BuildInfoError(f"missing or invalid {BUILD_INFO_FILENAME}")
    expected = {
        "category": context.category,
        "software": context.software,
        "requested_version": context.version,
        "actual_version": context.version,
        "architecture": context.architecture,
        "run_id": context.run_id,
    }
    expected_fields = set(expected) | {"recorded_at"}
    if set(payload) != expected_fields:
        raise BuildInfoError(
            f"{BUILD_INFO_FILENAME} fields must be exactly {sorted(expected_fields)}"
        )
    for field, expected_value in expected.items():
        if payload.get(field) != expected_value:
            raise BuildInfoError(
                f"{BUILD_INFO_FILENAME} field {field} must be {expected_value!r}"
            )
    if not isinstance(payload.get("recorded_at"), str) or not payload["recorded_at"]:
        raise BuildInfoError(f"{BUILD_INFO_FILENAME} recorded_at is missing")
    return payload
