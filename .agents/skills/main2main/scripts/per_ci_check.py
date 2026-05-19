#!/usr/bin/env python3
"""Pre-CI verification checks for main2main steps.

Runs three mechanical checks before CI to catch common multi-step errors:
  1. Version guard presence in changed files that touch vLLM interfaces
  2. Version string consistency across all vllm_version_is() calls
  3. No temporary/intermediate files in the repository working tree

Usage:
    python3 pre_ci_check.py \
      --ascend-path <path> \
      --release-tag <version>

Output (stdout):
    JSON with check results and overall pass/fail.
    Exits 0 if all checks pass, 1 if any check fails.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

TEMP_PATTERNS = [
    ".log",
    ".patch",
    ".jsonl",
    "vllm_changes.md",
    "vllm_error_analyze.md",
    "round-ledger",
    "main2main-failure-summary",
    "ci-summary",
]


def _run_git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout


def _check_version_guards(repo: Path) -> dict:
    """Check that changed files touching vLLM interfaces have version guards."""
    changed_output = _run_git(repo, "diff", "--name-only", "HEAD")
    staged_output = _run_git(repo, "diff", "--name-only", "--cached")
    changed = set()
    for line in (changed_output + staged_output).strip().splitlines():
        line = line.strip()
        if line and line.startswith("vllm_ascend/"):
            changed.add(line)

    files_without_guards: list[str] = []
    files_with_guards: list[str] = []

    for filepath in sorted(changed):
        full_path = repo / filepath
        if not full_path.exists() or not full_path.is_file():
            continue
        try:
            content = full_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        # Only check files that import from vllm (likely touching vLLM interfaces)
        if "from vllm" not in content and "import vllm" not in content:
            continue

        if "vllm_version_is" in content:
            files_with_guards.append(filepath)
        else:
            files_without_guards.append(filepath)

    return {
        "changed_vllm_files": sorted(changed),
        "files_with_guards": files_with_guards,
        "files_without_guards": files_without_guards,
    }


def _check_version_strings(repo: Path, release_tag: str) -> dict:
    """Check that all vllm_version_is() calls use a consistent version string."""
    result = subprocess.run(
        ["grep", "-rn", "vllm_version_is", str(repo / "vllm_ascend")],
        capture_output=True,
        text=True,
        check=False,
    )

    all_calls: list[str] = []
    mismatched: list[str] = []

    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        all_calls.append(line.strip())
        if "vllm_version_is" in line and release_tag not in line:
            # Skip import lines and definition lines
            if "import " in line or "def " in line:
                continue
            mismatched.append(line.strip())

    return {
        "release_tag": release_tag,
        "total_calls": len(all_calls),
        "mismatched": mismatched,
    }


def _check_temp_files(repo: Path) -> dict:
    """Check that no temporary files exist in the repository working tree."""
    status_output = _run_git(repo, "status", "--short")
    untracked_output = _run_git(repo, "ls-files", "--others", "--exclude-standard")

    all_files = set()
    for line in (status_output + untracked_output).strip().splitlines():
        # git status --short lines start with status codes
        filepath = line.strip().lstrip("MADRCU?! ").strip()
        if filepath:
            all_files.add(filepath)

    violations: list[str] = []
    for filepath in sorted(all_files):
        basename = Path(filepath).name
        for pattern in TEMP_PATTERNS:
            if pattern in basename or basename.endswith(pattern):
                violations.append(filepath)
                break

    return {
        "violations": violations,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run pre-CI verification checks for main2main.",
    )
    parser.add_argument("--ascend-path", type=Path, required=True,
                        help="Path to the vllm-ascend repository")
    parser.add_argument("--release-tag", required=True,
                        help="Expected release version string (main_vllm_tag from conf.py)")
    args = parser.parse_args()

    repo = args.ascend_path
    if not (repo / ".git").exists():
        print(f"Error: {repo} is not a git repository", file=sys.stderr)
        sys.exit(1)

    guards = _check_version_guards(repo)
    versions = _check_version_strings(repo, args.release_tag)
    temps = _check_temp_files(repo)

    all_passed = True
    checks: list[dict] = []

    # Check 1: version guards
    guard_ok = len(guards["files_without_guards"]) == 0
    checks.append({
        "name": "version_guards",
        "passed": guard_ok,
        "detail": (
            f"{len(guards['files_with_guards'])} files have guards"
            if guard_ok
            else f"{len(guards['files_without_guards'])} files import from vllm but lack vllm_version_is()"
        ),
        "files_without_guards": guards["files_without_guards"],
    })
    if not guard_ok:
        all_passed = False

    # Check 2: version string consistency
    version_ok = len(versions["mismatched"]) == 0
    checks.append({
        "name": "version_strings",
        "passed": version_ok,
        "detail": (
            f"all {versions['total_calls']} calls use {args.release_tag}"
            if version_ok
            else f"{len(versions['mismatched'])} calls use a different version string"
        ),
        "mismatched": versions["mismatched"],
    })
    if not version_ok:
        all_passed = False

    # Check 3: temp files
    temp_ok = len(temps["violations"]) == 0
    checks.append({
        "name": "temp_files",
        "passed": temp_ok,
        "detail": (
            "no temp files in repo"
            if temp_ok
            else f"{len(temps['violations'])} temp files found in repo"
        ),
        "violations": temps["violations"],
    })
    if not temp_ok:
        all_passed = False

    result = {
        "all_passed": all_passed,
        "checks": checks,
    }

    print(json.dumps(result, indent=2))
    sys.exit(0 if all_passed else 1)


if __name__ == "__main__":
    main()