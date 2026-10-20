#!/usr/bin/env python3
"""Temporary dependency checks for rc2 wheel builds, before compilation."""

import importlib.metadata
import os
import platform
import subprocess
from pathlib import Path

from packaging.requirements import Requirement


def dependency_errors(requirements):
    errors = []
    for text in requirements:
        if not text.strip() or text.lstrip().startswith("#"):
            continue
        requirement = Requirement(text)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            installed = importlib.metadata.version(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            errors.append(f"{requirement.name}: not installed")
            continue
        if not requirement.specifier.contains(installed, prereleases=True):
            errors.append(f"{requirement}: installed {installed}")
        print(f"{requirement.name}=={installed}")
    return errors


def main():
    root = Path(__file__).resolve().parents[4]
    errors = dependency_errors((root / "requirements.txt").read_text().splitlines())
    errors += dependency_errors(("torch==2.10.0", "torch-npu==2.10.0.post7"))
    if os.environ.get("SOC_VERSION") != "ascend310p1":
        errors += dependency_errors(
            (
                "triton-ascend==3.6.0+dev20260923230212",
                "memfabric-hybrid==1.3.0",
                "memcache-hybrid==1.3.0",
            )
        )
    print(f"Python: {platform.python_version()}, architecture: {platform.machine()}")
    print(f"SOC_VERSION: {os.environ.get('SOC_VERSION')}")
    if platform.python_version_tuple()[:2] not in (("3", "10"), ("3", "11"), ("3", "12")):
        errors.append("The rc2 snapshots require Python 3.10, 3.11 or 3.12")
    cann = Path("/usr/local/Ascend/ascend-toolkit/latest/compiler/version.info").read_text()
    print(f"CANN compiler:\n{cann}")
    if "Version=9.2.0" not in cann.splitlines():
        errors.append("Expected CANN 9.2.0")

    # Report the whole image, including preinstalled CANN tools, for review.
    # Only the project's declared dependencies and rc2 pins gate compilation.
    subprocess.run(["python3", "-m", "pip", "check"], check=False)
    if errors:
        raise RuntimeError("\n".join(errors))
    print("rc2 project dependencies and versions passed validation")


if __name__ == "__main__":
    main()
