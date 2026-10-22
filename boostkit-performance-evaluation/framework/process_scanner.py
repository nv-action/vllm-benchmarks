#!/usr/bin/env python3
"""Find processes isolated to performance runs or referencing the work root."""

from __future__ import annotations

import argparse
import os
from pathlib import Path


def references_root(command_line: bytes, root: Path) -> bool:
    root_bytes = os.fsencode(str(root))
    prefix = root_bytes.rstrip(b"/") + b"/"
    arguments = [argument for argument in command_line.split(b"\0") if argument]
    return any(argument == root_bytes or prefix in argument for argument in arguments)


def environment_references_run(environment: bytes, root: Path) -> bool:
    entries = [entry for entry in environment.split(b"\0") if entry]
    if any(entry.startswith(b"PERF_PROCESS_TOKEN=boostkit-perf:") for entry in entries):
        return True
    work_prefix = b"PERF_WORK_DIR="
    return any(
        entry.startswith(work_prefix) and references_root(entry[len(work_prefix):], root)
        for entry in entries
    )


def linked_path_references_root(path: Path, root: Path) -> bool:
    try:
        target = os.readlink(path)
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return False
    target = target.removesuffix(" (deleted)")
    root_text = str(root).rstrip("/")
    return target == root_text or target.startswith(f"{root_text}/")


def process_references_run(process_dir: Path, root: Path) -> bool:
    try:
        command_line = (process_dir / "cmdline").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        return False
    if command_line and references_root(command_line, root):
        return True
    try:
        environment = (process_dir / "environ").read_bytes()
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        environment = b""
    if environment_references_run(environment, root):
        return True
    if any(linked_path_references_root(process_dir / name, root) for name in ("cwd", "exe")):
        return True
    try:
        file_descriptors = list((process_dir / "fd").iterdir())
    except (FileNotFoundError, PermissionError, ProcessLookupError, OSError):
        file_descriptors = []
    return any(linked_path_references_root(descriptor, root) for descriptor in file_descriptors)


def matching_processes(root: Path, excluded: set[int] | None = None) -> list[int]:
    excluded_pids = set(excluded or ())
    excluded_pids.update({os.getpid(), os.getppid()})
    matches: list[int] = []
    for process_dir in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(process_dir.name)
        except ValueError:
            continue
        if pid in excluded_pids:
            continue
        if process_references_run(process_dir, root):
            matches.append(pid)
    return sorted(matches)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--exclude", action="append", default=[], type=int)
    args = parser.parse_args()
    for pid in matching_processes(args.root, set(args.exclude)):
        print(pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
