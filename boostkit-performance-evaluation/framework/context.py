#!/usr/bin/env python3
"""Immutable context passed to one software performance task."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class RunContext:
    root: Path
    case_path: Path
    case: dict[str, Any]
    category: str
    software: str
    version: str
    architecture: str
    run_id: str
    output_dir: Path
    work_dir: Path

    @property
    def case_dir(self) -> Path:
        return self.case_path.parent

    @property
    def execution(self) -> dict[str, Any]:
        return dict(self.case.get("execution", {}))
