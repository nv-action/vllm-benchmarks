#!/usr/bin/env python3
#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
#
"""
Helper script for bisect_vllm.sh.

Subcommands:
  get-commit   - Extract vllm commit hash from a workflow yaml file.
  report       - Generate a markdown bisect report.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import re

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_RUNNER = "linux-aarch64-a3-4"
DEFAULT_IMAGE = "m.daocloud.io/quay.io/ascend/cann:8.5.1-a3-ubuntu22.04-py3.11"

PATH_KIND_RULES = [
    {"pattern": r"tests/e2e/310p/multicard/", "kind": "e2e_310p_4cards"},
    {"pattern": r"tests/e2e/310p/singlecard/", "kind": "e2e_310p_singlecard"},
    {"pattern": r"tests/e2e/multicard/4-cards/", "kind": "e2e_4cards"},
    {"pattern": r"tests/e2e/multicard/2-cards/", "kind": "e2e_2cards"},
    {"pattern": r"tests/e2e/singlecard/", "kind": "e2e_singlecard_full"},
    {"pattern": r"tests/ut/", "kind": "ut"},
]

KIND_SOURCES = {
    "ut": {
        "test_type": "ut",
        "workflow": ".github/workflows/_unit_test.yaml",
        "job": "unit-test",
        "caller_workflow": ".github/workflows/pr_test_light.yaml",
        "caller_job": "ut",
        "prepare_steps": ["Install packages"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run unit test",
    },
    "e2e_singlecard_full": {
        "test_type": "e2e",
        "workflow": ".github/workflows/_e2e_test.yaml",
        "job": "e2e-full",
        "caller_workflow": ".github/workflows/pr_test_full.yaml",
        "caller_job": "e2e-test",
        "prepare_steps": ["Config mirrors", "Install system dependencies"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run e2e test",
    },
    "e2e_2cards": {
        "test_type": "e2e",
        "workflow": ".github/workflows/_e2e_test.yaml",
        "job": "e2e-2-cards-full",
        "prepare_steps": ["Config mirrors", "Install system dependencies"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run nv-action/vllm-benchmark test (full)",
    },
    "e2e_4cards": {
        "test_type": "e2e",
        "workflow": ".github/workflows/_e2e_test.yaml",
        "job": "e2e-4-cards-full",
        "prepare_steps": ["Config mirrors", "Install system dependencies"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run nv-action/vllm-benchmark test for V1 Engine",
    },
    "e2e_310p_singlecard": {
        "test_type": "e2e",
        "workflow": ".github/workflows/_e2e_test.yaml",
        "job": "e2e_310p",
        "prepare_steps": ["Config mirrors", "Install system dependencies"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run nv-action/vllm-benchmark test",
    },
    "e2e_310p_4cards": {
        "test_type": "e2e",
        "workflow": ".github/workflows/_e2e_test.yaml",
        "job": "e2e_310p-4cards",
        "prepare_steps": ["Config mirrors", "Install system dependencies"],
        "vllm_install_step": "Install vllm-project/vllm from source",
        "ascend_install_step": "Install nv-action/vllm-benchmark",
        "test_step": "Run nv-action/vllm-benchmark test",
    },
}

_MANIFEST_CACHE: dict[str, dict] | None = None

# Regex to match a 7+ hex-char commit hash (not a vX.Y.Z tag)
COMMIT_HASH_RE = re.compile(r"^[0-9a-f]{7,40}$")

# Regex to extract test file path from pytest command
TEST_PATH_RE = re.compile(r"\b(tests/[-\w/]+\.py(?:::[\w_]+)*)")


def _resolve_env_for_test_cmd(test_cmd: str) -> dict:
    """Resolve full environment config based on the test file path in test_cmd."""
    manifest = load_runtime_env_manifest()
    for rule in PATH_KIND_RULES:
        if re.search(rule["pattern"], test_cmd):
            env = manifest.get(rule["kind"])
            if env:
                return env
    return {"runner": DEFAULT_RUNNER, "image": DEFAULT_IMAGE, "test_type": "e2e"}


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _parse_scalar(value: str):
    value = value.strip()
    if value == "":
        return ""
    if (value.startswith("'") and value.endswith("'")) or (value.startswith('"') and value.endswith('"')):
        return value[1:-1]
    if value in {"true", "True"}:
        return True
    if value in {"false", "False"}:
        return False
    if re.fullmatch(r"\[[^\]]*\]", value):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    return value


def _next_content_line(lines: list[str], start: int) -> tuple[int | None, str | None]:
    i = start
    while i < len(lines):
        stripped = lines[i].strip()
        if stripped and not stripped.startswith("#"):
            return i, lines[i]
        i += 1
    return None, None


def _parse_block_scalar(lines: list[str], start: int, indent: int) -> tuple[str, int]:
    collected: list[str] = []
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        current_indent = _line_indent(raw)
        if stripped == "":
            collected.append("")
            i += 1
            continue
        if current_indent < indent:
            break
        collected.append(raw[indent:])
        i += 1
    return "\n".join(collected).rstrip(), i


def _parse_mapping(lines: list[str], start: int, indent: int) -> tuple[dict, int]:
    data: dict = {}
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        current_indent = _line_indent(raw)
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        if current_indent < indent or raw[current_indent:].startswith("- "):
            break
        if current_indent != indent:
            break

        key, sep, remainder = raw[indent:].partition(":")
        if not sep:
            break
        remainder = remainder.lstrip()
        if remainder == "|":
            value, i = _parse_block_scalar(lines, i + 1, indent + 2)
        elif remainder == "":
            next_idx, next_line = _next_content_line(lines, i + 1)
            if next_idx is None:
                value = {}
                i += 1
            else:
                next_indent = _line_indent(next_line)
                if next_indent <= indent:
                    value = {}
                    i += 1
                elif next_line[next_indent:].startswith("- "):
                    value, i = _parse_sequence(lines, i + 1, indent + 2)
                else:
                    value, i = _parse_mapping(lines, i + 1, indent + 2)
        else:
            value = _parse_scalar(remainder)
            i += 1
        data[key.strip()] = value
    return data, i


def _parse_sequence(lines: list[str], start: int, indent: int) -> tuple[list, int]:
    items: list = []
    i = start
    while i < len(lines):
        raw = lines[i]
        stripped = raw.strip()
        current_indent = _line_indent(raw)
        if stripped == "" or stripped.startswith("#"):
            i += 1
            continue
        if current_indent < indent or not raw[indent:].startswith("- "):
            break

        inline = raw[indent + 2 :]
        item: dict | str
        if ":" in inline:
            key, _, remainder = inline.partition(":")
            key = key.strip()
            remainder = remainder.lstrip()
            item = {}
            if remainder == "|":
                value, i = _parse_block_scalar(lines, i + 1, indent + 4)
            elif remainder == "":
                next_idx, next_line = _next_content_line(lines, i + 1)
                if next_idx is None:
                    value = {}
                    i += 1
                else:
                    next_indent = _line_indent(next_line)
                    if next_indent <= indent + 2:
                        value = {}
                        i += 1
                    elif next_line[next_indent:].startswith("- "):
                        value, i = _parse_sequence(lines, i + 1, indent + 4)
                    else:
                        value, i = _parse_mapping(lines, i + 1, indent + 4)
            else:
                value = _parse_scalar(remainder)
                i += 1
            item[key] = value
            extra, i = _parse_mapping(lines, i, indent + 2)
            item.update(extra)
        else:
            item = _parse_scalar(inline)
            i += 1
        items.append(item)
    return items, i


def _load_yaml(path: Path) -> dict:
    lines = path.read_text(encoding="utf-8").splitlines()
    data, _ = _parse_mapping(lines, 0, 0)
    return data


def _resolve_inputs(value, with_inputs: dict[str, object] | None):
    if isinstance(value, str) and with_inputs:
        match = re.fullmatch(r"\${{\s*inputs\.([A-Za-z0-9_]+)\s*}}", value)
        if match:
            return with_inputs.get(match.group(1), value)
    if isinstance(value, dict):
        return {k: _resolve_inputs(v, with_inputs) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_inputs(v, with_inputs) for v in value]
    return value


def _normalize_env(env: dict | None) -> dict[str, str]:
    if not isinstance(env, dict):
        return {}
    return {str(k): str(v) for k, v in env.items()}


def _merge_envs(*envs: dict[str, str]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for env in envs:
        merged.update(env)
    return merged


def _format_run_with_env(run: str | None, env: dict[str, str] | None = None) -> str:
    if not run:
        return ""
    env = env or {}
    exports = [f"export {key}={shlex.quote(value)}" for key, value in env.items()]
    body = run.strip()
    return "\n".join(exports + [body]) if exports else body


def _get_job(workflow: dict, job_name: str) -> dict:
    jobs = workflow.get("jobs", {})
    if not isinstance(jobs, dict) or job_name not in jobs:
        raise KeyError(f"job not found: {job_name}")
    job = jobs[job_name]
    if not isinstance(job, dict):
        raise TypeError(f"job is not a mapping: {job_name}")
    return job


def _get_step(job: dict, step_name: str) -> dict:
    for step in job.get("steps", []):
        if isinstance(step, dict) and step.get("name") == step_name:
            return step
    raise KeyError(f"step not found: {step_name}")


def _get_caller_inputs(caller_workflow_path: str | None, caller_job_name: str | None) -> dict[str, object]:
    if not caller_workflow_path or not caller_job_name:
        return {}
    workflow = _load_yaml(REPO_ROOT / caller_workflow_path)
    job = _get_job(workflow, caller_job_name)
    with_inputs = job.get("with", {})
    return with_inputs if isinstance(with_inputs, dict) else {}


def _extract_profile(kind: str, config: dict) -> dict:
    workflow = _load_yaml(REPO_ROOT / config["workflow"])
    caller_inputs = _get_caller_inputs(config.get("caller_workflow"), config.get("caller_job"))
    job = _get_job(workflow, config["job"])
    workflow_env = _normalize_env(workflow.get("env"))
    container = job.get("container", {}) if isinstance(job.get("container"), dict) else {}
    container_env = _normalize_env(_resolve_inputs(container.get("env", {}), caller_inputs))
    effective_container_env = _merge_envs(workflow_env, container_env)

    prepare_runs = []
    for step_name in config.get("prepare_steps", []):
        prepare_step = _get_step(job, step_name)
        prepare_runs.append(_format_run_with_env(_resolve_inputs(prepare_step.get("run", ""), caller_inputs)))
    vllm_install_step = _get_step(job, config["vllm_install_step"])
    ascend_install_step = _get_step(job, config["ascend_install_step"])
    test_step = _get_step(job, config["test_step"])

    step_env = _normalize_env(_resolve_inputs(test_step.get("env", {}), caller_inputs))
    runtime_env = _merge_envs(effective_container_env, step_env)

    profile = {
        "kind": kind,
        "test_type": config["test_type"],
        "runner": str(_resolve_inputs(job.get("runs-on", DEFAULT_RUNNER), caller_inputs)),
        "image": str(_resolve_inputs(container.get("image", DEFAULT_IMAGE), caller_inputs)),
        "workflow_env": workflow_env,
        "container_env": container_env,
        "effective_container_env": effective_container_env,
        "sys_deps": "\n".join(part for part in prepare_runs if part),
        "vllm_install": _format_run_with_env(_resolve_inputs(vllm_install_step.get("run", ""), caller_inputs)),
        "ascend_install": _format_run_with_env(
            _resolve_inputs(ascend_install_step.get("run", ""), caller_inputs),
            _normalize_env(_resolve_inputs(ascend_install_step.get("env", {}), caller_inputs)),
        ),
        "runtime_env": runtime_env,
        "test_step": {
            "name": str(test_step.get("name", "")),
            # "if": str(test_step.get("if", "")) if test_step.get("if") is not None else "",
            "shell": str(test_step.get("shell", "")) if test_step.get("shell") is not None else "",
            "env": step_env,
            "run": str(_resolve_inputs(test_step.get("run", ""), caller_inputs)),
        },
    }
    return profile


def load_runtime_env_manifest() -> dict[str, dict]:
    global _MANIFEST_CACHE
    if _MANIFEST_CACHE is None:
        _MANIFEST_CACHE = {kind: _extract_profile(kind, config) for kind, config in KIND_SOURCES.items()}
    return _MANIFEST_CACHE


def get_commit_from_yaml(yaml_path: str, ref: str | None = None) -> str | None:
    """Extract vllm commit hash from a workflow yaml file.

    Reads the file content either from disk (ref=None) or from a git ref
    (e.g. ref='origin/main') via ``git show ref:path``.

    Looks for the vllm_version matrix pattern like:
        vllm_version: [<commit_hash>, v0.15.0]
    and returns the commit hash entry (the one that is NOT a vX.Y.Z tag).
    """
    if ref:
        # Read from git ref
        try:
            # Compute relative path from repo root
            repo_root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
            ).strip()
            rel_path = os.path.relpath(yaml_path, repo_root)
            content = subprocess.check_output(
                ["git", "show", f"{ref}:{rel_path}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except subprocess.CalledProcessError:
            return None
    else:
        try:
            content = Path(yaml_path).read_text()
        except FileNotFoundError:
            return None

    # Match patterns like: vllm_version: [abc123, v0.15.0]
    # or multi-line matrix definitions
    match = re.search(
        r"vllm_version:\s*\[([^\]]+)\]",
        content,
    )
    if not match:
        return None

    entries = [e.strip().strip("'\"") for e in match.group(1).split(",")]
    for entry in entries:
        if COMMIT_HASH_RE.match(entry):
            return entry
    return None


def get_pkg_location(pkg_name: str) -> str | None:
    """Get package install location via pip show.

    For editable installs, prefers ``Editable project location`` which
    points directly to the source tree.  Falls back to ``Location``
    (site-packages directory) for regular installs.
    """
    try:
        output = subprocess.check_output(
            ["pip", "show", pkg_name],
            text=True,
            stderr=subprocess.DEVNULL,
        )
        editable_loc = None
        location = None
        for line in output.splitlines():
            if line.startswith("Editable project location:"):
                editable_loc = line.split(":", 1)[1].strip()
            elif line.startswith("Location:"):
                location = line.split(":", 1)[1].strip()
        # Prefer editable location (source tree) over site-packages
        return editable_loc or location
    except (subprocess.CalledProcessError, FileNotFoundError):
        pass
    return None


def generate_report(
    bad_commit: str,
    good_commit: str,
    first_bad: str,
    first_bad_info: str,
    test_cmd: str,
    total_steps: int,
    total_commits: int,
    skipped: list[str] | None = None,
    log_entries: list[dict] | None = None,
) -> str:
    """Generate a markdown bisect report."""
    lines = [
        "## Bisect Result",
        "",
        "| Field | Value |",
        "|-------|-------|",
        f"| First bad commit | `{first_bad}` |",
        f"| Link | https://github.com/vllm-project/vllm/commit/{first_bad} |",
        f"| Good commit | `{good_commit}` |",
        f"| Bad commit | `{bad_commit}` |",
        f"| Range | {total_commits} commits, {total_steps} bisect steps |",
        f"| Test command | `{test_cmd}` |",
        "",
        "### First Bad Commit Details",
        "```",
        first_bad_info,
        "```",
    ]

    if skipped:
        lines += [
            "",
            "### Skipped Commits",
            "",
        ]
        for s in skipped:
            lines.append(f"- `{s}`")

    if log_entries:
        lines += [
            "",
            "### Bisect Log",
            "",
            "| Step | Commit | Result |",
            "|------|--------|--------|",
        ]
        for i, entry in enumerate(log_entries, 1):
            lines.append(f"| {i} | `{entry.get('commit', '?')[:12]}` | {entry.get('result', '?')} |")

    lines += [
        "",
        "---",
        "*Generated by `.github/workflows/scripts/bisect_vllm.sh`*",
    ]
    return "\n".join(lines)


def build_batch_matrix(test_cmds_str: str) -> dict:
    """Parse semicolon-separated test commands and group by (runner, image, test_type).

    Returns a GitHub Actions matrix JSON object with an "include" array.
    Each element contains the full environment config needed by the workflow:
    group, runner, image, test_type, test_cmds, container_env, sys_deps,
    vllm_install, ascend_install, runtime_env.
    """
    cmds = [c.strip() for c in test_cmds_str.split(";") if c.strip()]
    if not cmds:
        return {"include": []}

    manifest = load_runtime_env_manifest()
    all_container_env_keys = sorted(
        {
            key
            for profile in manifest.values()
            for key in profile.get("effective_container_env", {})
        }
    )

    # Group by (runner, image, test_type) — commands sharing the same env
    groups: dict[tuple[str, str, str], list[str]] = {}
    group_env: dict[tuple[str, str, str], dict] = {}
    for cmd in cmds:
        env = _resolve_env_for_test_cmd(cmd)
        key = (env["runner"], env["image"], env["test_type"])
        groups.setdefault(key, []).append(cmd)
        if key not in group_env:
            group_env[key] = env
        else:
            # Merge container_env and runtime_env from all commands in group
            for field in ("effective_container_env", "runtime_env"):
                existing = group_env[key].get(field, {})
                existing.update(env.get(field, {}))
                group_env[key][field] = existing

    # Build matrix include array
    include = []
    for (runner, image, test_type), group_cmds in groups.items():
        env = group_env[(runner, image, test_type)]
        group_name = f"{test_type}-{runner.split('-')[-1]}"

        # Flatten effective container env into individual matrix keys (for YAML static refs)
        # Fill all known keys with empty string if not present in this env
        container_env = env.get("effective_container_env", {})
        entry = {
            "group": group_name,
            "runner": runner,
            "image": image,
            "test_type": test_type,
            "test_cmds": ";".join(group_cmds),
            "sys_deps": env.get("sys_deps", "echo 'no sys_deps configured'"),
            "vllm_install": env.get("vllm_install", "echo 'no vllm_install configured'"),
            "ascend_install": env.get("ascend_install", "echo 'no ascend_install configured'"),
            "runtime_env": json.dumps(env.get("runtime_env", {})),
        }
        # Add each container_env key as a top-level matrix field (cenv_XXX)
        for k in all_container_env_keys:
            entry[f"cenv_{k}"] = container_env.get(k, "")

        include.append(entry)

    include.sort(
        key=lambda e: (
            e["test_type"],
            int(re.search(r"-(\d+)$", e["group"]).group(1)) if re.search(r"-(\\d+)$", e["group"]) else 9999,
            e["group"],
        )
    )
    return {"include": include}

def cmd_batch_matrix(args):
    matrix = build_batch_matrix(args.test_cmds)
    matrix_json = json.dumps(matrix, separators=(",", ":"))
    if args.output_format == "github":
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a") as f:
                f.write(f"matrix={matrix_json}\n")
        print(f"matrix={matrix_json}")
        total_cmds = sum(len(g["test_cmds"].split(";")) for g in matrix["include"])
        print(f"Total: {len(matrix['include'])} group(s) from {total_cmds} command(s)")
    else:
        print(json.dumps(matrix, indent=2))


def cmd_get_commit(args):
    yaml_path = args.yaml_path
    if not yaml_path:
        # Default: pr_test_light.yaml relative to this script's repo
        try:
            repo_root = subprocess.check_output(
                ["git", "rev-parse", "--show-toplevel"],
                text=True,
            ).strip()
            yaml_path = os.path.join(repo_root, ".github/workflows/pr_test_light.yaml")
        except subprocess.CalledProcessError:
            print("ERROR: Cannot determine repo root", file=sys.stderr)
            sys.exit(1)

    commit = get_commit_from_yaml(yaml_path, ref=args.ref)
    if commit:
        print(commit)
    else:
        print(
            f"ERROR: Could not extract vllm commit from {yaml_path}" + (f" at ref {args.ref}" if args.ref else ""),
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_report(args):
    skipped = args.skipped.split(",") if args.skipped else None
    log_entries = None
    if args.log_file:
        try:
            with open(args.log_file) as f:
                log_entries = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # Read first_bad_info from file or argument
    first_bad_info = args.first_bad_info or ""
    if args.first_bad_info_file:
        try:
            first_bad_info = Path(args.first_bad_info_file).read_text().strip()
        except FileNotFoundError:
            first_bad_info = "N/A"

    report = generate_report(
        bad_commit=args.bad_commit,
        good_commit=args.good_commit,
        first_bad=args.first_bad,
        first_bad_info=first_bad_info,
        test_cmd=args.test_cmd,
        total_steps=args.total_steps,
        total_commits=args.total_commits,
        skipped=skipped,
        log_entries=log_entries,
    )
    print(report)

    # Write to unified bisect_summary.md file (for artifact upload)
    summary_md_path = Path("/tmp/bisect_summary.md")
    with open(summary_md_path, "a" if summary_md_path.exists() else "w", encoding="utf-8") as f:
        match = TEST_PATH_RE.search(args.test_cmd)
        test_path = match.group(1) if match else args.test_cmd
        f.write(f"\n# bisect {test_path}\n\n")
        f.write(report + "\n")

    # Write to GITHUB_STEP_SUMMARY if available
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_file:
        with open(summary_file, "a") as f:
            f.write(report + "\n")


def cmd_vllm_location(args):
    loc = get_pkg_location("vllm")
    if loc:
        print(loc)
    else:
        print("ERROR: vllm not installed or pip show failed", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Helper for vllm bisect automation")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # batch-matrix
    p_batch = subparsers.add_parser(
        "batch-matrix",
        help="Build a GitHub Actions matrix from semicolon-separated test commands",
    )
    p_batch.add_argument(
        "--test-cmds",
        required=True,
        help="Semicolon-separated test commands",
    )
    p_batch.add_argument(
        "--output-format",
        choices=["json", "github"],
        default="github",
        help="Output format (default: github)",
    )
    p_batch.set_defaults(func=cmd_batch_matrix)

    # get-commit
    p_commit = subparsers.add_parser("get-commit", help="Extract vllm commit from workflow yaml")
    p_commit.add_argument(
        "--yaml-path",
        default="",
        help="Path to workflow yaml (default: pr_test_light.yaml)",
    )
    p_commit.add_argument(
        "--ref",
        default=None,
        help="Git ref to read from (e.g. origin/main). If unset, reads from disk.",
    )
    p_commit.set_defaults(func=cmd_get_commit)

    # report
    p_report = subparsers.add_parser("report", help="Generate bisect result report")
    p_report.add_argument("--good-commit", required=True)
    p_report.add_argument("--bad-commit", required=True)
    p_report.add_argument("--first-bad", required=True)
    p_report.add_argument(
        "--first-bad-info", default=None, help="Commit info string (mutually exclusive with --first-bad-info-file)"
    )
    p_report.add_argument("--first-bad-info-file", default=None, help="File containing commit info")
    p_report.add_argument("--test-cmd", required=True)
    p_report.add_argument("--total-steps", type=int, required=True)
    p_report.add_argument("--total-commits", type=int, required=True)
    p_report.add_argument("--skipped", default=None, help="Comma-separated skipped commits")
    p_report.add_argument("--log-file", default=None, help="Path to bisect log JSON file")
    p_report.set_defaults(func=cmd_report)

    # vllm-location
    p_loc = subparsers.add_parser("vllm-location", help="Get vllm install location via pip show")
    p_loc.set_defaults(func=cmd_vllm_location)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
