"""Fail private profiling validation if checkout or import paths miss the PR."""

import argparse
import importlib.util
import subprocess
import sys
from pathlib import Path


def verify_source(root: Path, ref: str, *, tools_only: bool = False) -> None:
    root = root.resolve()
    actual = subprocess.check_output(["git", "-C", str(root), "rev-parse", "HEAD"], text=True).strip()
    print(f"[Profiling source] expected={ref} actual={actual} python={sys.executable}", flush=True)
    if actual != ref:
        raise RuntimeError("Profiling checkout does not match the requested PR SHA")
    modules = ["tools.profiling.capture", "tools.profiling.steady_state", "tools.profiling.parse", "tools.aisbench"]
    if not tools_only:
        modules.append("vllm_ascend")
    for name in modules:
        spec = importlib.util.find_spec(name)
        origin = Path(spec.origin).resolve() if spec and spec.origin else None
        print(f"[Profiling source] {name}: {origin}", flush=True)
        if origin is None or not origin.is_relative_to(root):
            raise RuntimeError(f"{name} is not loaded from the requested checkout")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--tools-only", action="store_true")
    args = parser.parse_args()
    sys.path.insert(0, str(args.root.resolve()))
    verify_source(args.root, args.ref, tools_only=args.tools_only)
