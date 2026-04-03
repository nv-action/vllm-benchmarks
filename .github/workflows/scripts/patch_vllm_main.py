#!/usr/bin/env python3
"""Patch upstream vLLM main branch to fix known import errors.

This script is run after vllm-empty is checked out and installed.
It applies targeted patches for bugs in upstream vLLM main that
have not yet been fixed upstream.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path


def patch_env_override_graph_capture(repo_dir: Path) -> bool:
    """Fix GraphCaptureOutput import error in vllm/env_override.py.

    The import is guarded by ``is_torch_equal_or_newer("2.12.0")`` but
    ``GraphCaptureOutput`` may not exist in certain torch versions.
    Wrap the import in a try/except so the module loads regardless
    of torch version.

    Upstream commit that introduced this: e319150
    """
    env_override = repo_dir / "vllm" / "env_override.py"
    if not env_override.is_file():
        return False

    content = env_override.read_text()

    # Check if this specific pattern exists (not yet fixed)
    old_pattern = """if not is_torch_equal_or_newer("2.12.0"):
    import builtins as _builtins
    import pickle

    from torch._dynamo.convert_frame import GraphCaptureOutput

    _original_get_runtime_env = GraphCaptureOutput.get_runtime_env"""

    if old_pattern not in content:
        # Already fixed or has a different version
        return False

    new_pattern = """if not is_torch_equal_or_newer("2.12.0"):
    import builtins as _builtins
    import pickle

    try:
        from torch._dynamo.convert_frame import GraphCaptureOutput
    except ImportError:
        GraphCaptureOutput = None

    if GraphCaptureOutput is not None:
        _original_get_runtime_env = GraphCaptureOutput.get_runtime_env"""

    # Close the new conditional block before _patched_get_runtime_env
    # We need to indent the rest of the block content under `if GraphCaptureOutput is not None:`
    content = content.replace(
        """    GraphCaptureOutput.get_runtime_env = _patched_get_runtime_env""",
        """        GraphCaptureOutput.get_runtime_env = _patched_get_runtime_env""",
        1,
    )

    content = content.replace(old_pattern, new_pattern, 1)
    env_override.write_text(content)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply patches to the upstream vLLM checkout"
    )
    parser.add_argument(
        "--vllm-repo",
        required=True,
        help="Path to the vllm-empty checkout directory",
    )
    args = parser.parse_args()
    repo_dir = Path(args.vllm_repo)

    if not repo_dir.is_dir():
        print(f"Error: vllm repo directory not found: {repo_dir}", file=sys.stderr)
        return 1

    applied = []
    if patch_env_override_graph_capture(repo_dir):
        applied.append("env_override GraphCaptureOutput safe import")

    if applied:
        print(f"Applied {len(applied)} patch(es):")
        for p in applied:
            print(f"  - {p}")
    else:
        print("No patches applied (already fixed or not applicable).")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
