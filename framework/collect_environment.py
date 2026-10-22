"""Collect immutable system identity once and mutable runtime state twice."""

from __future__ import annotations

import os
import platform
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path


def _command(args: list[str]) -> str:
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=10, check=False)
        return result.stdout.strip() if result.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


def _cpu_model() -> str:
    lscpu = _command([
        "/usr/bin/sudo",
        "-n",
        "/usr/bin/env",
        "LC_ALL=C",
        "/usr/bin/lscpu",
    ])
    for line in lscpu.splitlines():
        field, separator, value = line.partition(":")
        if separator and field.strip().casefold() == "model name":
            model_name = value.strip()
            if model_name:
                return model_name
    return "unknown"


def _os_pretty_name() -> str:
    try:
        os_release = Path("/etc/os-release").read_text(
            encoding="utf-8", errors="replace"
        )
    except OSError:
        return "ERROR"
    for line in os_release.splitlines():
        field, separator, value = line.partition("=")
        if separator and field.strip() == "PRETTY_NAME":
            try:
                parsed = shlex.split(value, posix=True)
            except ValueError:
                return "ERROR"
            if parsed:
                return " ".join(parsed)
            return "ERROR"
    return "ERROR"


def _gcc_version() -> str:
    value = _command(["gcc", "-dumpfullversion", "-dumpversion"])
    return value.splitlines()[0] if value else "unknown"


def _glibc_version() -> str:
    value = _command(["getconf", "GNU_LIBC_VERSION"])
    if value:
        return value.splitlines()[0]
    implementation, version = platform.libc_ver()
    if implementation and version:
        return f"{implementation} {version}"
    return "unknown"


def _collected_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect_system_info() -> dict:
    """Collect values expected to remain stable for the entire performance task."""
    return {
        "collected_at": _collected_at(),
        "architecture": platform.machine(),
        "platform": _os_pretty_name(),
        "kernel": platform.release(),
        "cpu_model": _cpu_model(),
        "cpu_count": os.cpu_count() or 0,
        "python_version": platform.python_version(),
        "gcc_version": _gcc_version(),
        "glibc_version": _glibc_version(),
        "numa": _command(["numactl", "--hardware"]),
    }


def collect_runtime_state() -> dict:
    """Collect values that can change while a performance test is running."""
    return {
        "collected_at": _collected_at(),
        "memory": _command(["free", "-b"]),
        "cpu_governor": _command([
            "sh",
            "-c",
            "cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_governor 2>/dev/null",
        ]),
    }
