# Copyright (c) 2026 Huawei Technologies Co., Ltd.
"""Observe local-action visibility without requiring access to the runner host."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import textwrap
import time
from pathlib import Path


def action_manifest(token: str) -> str:
    return textwrap.dedent("""\
        name: Runtime synchronization marker
        description: Diagnostic fixture; not a production action transport.
        outputs:
          token:
            value: ${{ steps.loaded.outputs.token }}
          manifest-sha:
            value: ${{ steps.loaded.outputs.manifest-sha }}
        runs:
          using: composite
          steps:
            - id: loaded
              shell: bash
              env:
                SYNC_LOADED_TOKEN: TOKEN_PLACEHOLDER
                SYNC_ACTION_PATH: ${{ github.action_path }}
              run: |
                set -euo pipefail
                printf 'Loaded action token=%s; action_path=%s\\n' "$SYNC_LOADED_TOKEN" "$SYNC_ACTION_PATH"
                printf 'token=%s\\n' "$SYNC_LOADED_TOKEN" >> "$GITHUB_OUTPUT"
                test -f "$SYNC_ACTION_PATH/action.yaml"
                manifest_sha="$(sha256sum "$SYNC_ACTION_PATH/action.yaml" | cut -d ' ' -f 1)"
                printf 'manifest-sha=%s\\n' "$manifest_sha" >> "$GITHUB_OUTPUT"
        """).replace("TOKEN_PLACEHOLDER", json.dumps(token))


def target_directory(location: str, workspace: Path, runner_temp: Path) -> Path:
    relatives = {
        "workspace": "action-sync-probe",
        "hidden": ".action-sync-probe",
        "temp": "../../_temp/action-sync-probe",
        "commands": "../../_temp/_runner_file_commands/action-sync-probe",
    }
    directory = (workspace / relatives[location]).resolve()
    if location in {"temp", "commands"}:
        parent = runner_temp if location == "temp" else runner_temp / "_runner_file_commands"
        if directory != (parent / "action-sync-probe").resolve():
            raise ValueError("RUNNER_TEMP does not match the static uses path; this cell cannot test that path")
    return directory


def stage(location: str, phase: str, workspace: Path, runner_temp: Path, run_identity: str) -> dict:
    directory = target_directory(location, workspace, runner_temp)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o755)
    manifest = directory / "action.yaml"
    previous = hashlib.sha256(manifest.read_bytes()).hexdigest() if manifest.is_file() else None
    token = f"{run_identity}-{location}-{phase}"
    manifest.write_text(action_manifest(token), encoding="utf-8")
    manifest.chmod(0o644)
    stat = manifest.stat()
    return {
        "location": location,
        "phase": phase,
        "token": token,
        "sha256": hashlib.sha256(manifest.read_bytes()).hexdigest(),
        "previous_sha256": previous,
        "written_at_ns": time.time_ns(),
        "manifest_path": str(manifest),
        "workspace": str(workspace),
        "runner_temp": str(runner_temp),
        "mode": oct(stat.st_mode & 0o777),
        "uid": stat.st_uid,
        "gid": stat.st_gid,
    }


def classify(first: dict, retry: dict, phases: dict) -> str:
    first_ok = first.get("outcome") == "success"
    retry_ok = retry.get("outcome") == "success"
    if first_ok and first.get("outputs", {}).get("token") != phases["v1"]["token"]:
        raise ValueError("The immediate action loaded an unexpected marker")
    if not retry_ok:
        return "became-unavailable" if first_ok else "unavailable-at-both-loads"
    token = retry.get("outputs", {}).get("token")
    if token == phases["v1"]["token"]:
        return "stale-v1-after-v2-write"
    if token != phases["v2"]["token"]:
        raise ValueError("The retry loaded an unexpected marker")
    return "current-at-both-loads" if first_ok else "visible-after-intervening-step"


def report(output: Path, first: dict, retry: dict, environment: str) -> dict:
    phases = {phase: json.loads((output / f"{phase}.json").read_text()) for phase in ("v1", "v2")}
    result = {
        "environment": environment,
        "location": phases["v1"]["location"],
        "phases": phases,
        "first": first,
        "retry": retry,
        "classification": classify(first, retry, phases),
    }
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("stage", "report"))
    parser.add_argument("--phase", choices=("v1", "v2"))
    args = parser.parse_args()
    workspace = Path(os.environ["GITHUB_WORKSPACE"]).resolve()
    output = workspace / "action-sync-results"
    output.mkdir(exist_ok=True)
    environment = os.environ["SYNC_PROBE_ENVIRONMENT"]
    if args.operation == "stage":
        if args.phase is None:
            parser.error("--phase is required for stage")
        record = stage(
            os.environ["SYNC_PROBE_LOCATION"],
            args.phase,
            workspace,
            Path(os.environ["RUNNER_TEMP"]).resolve(),
            f"{os.environ['GITHUB_RUN_ID']}-{os.environ['GITHUB_RUN_ATTEMPT']}",
        )
        (output / f"{args.phase}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(record, indent=2), flush=True)
        return
    result = report(
        output,
        json.loads(os.environ["SYNC_PROBE_FIRST"]),
        json.loads(os.environ["SYNC_PROBE_RETRY"]),
        environment,
    )
    print(json.dumps(result, indent=2), flush=True)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"### {environment} / {result['location']}\n\n")
        summary.write(f"Observation: **{result['classification']}**\n\n")
        summary.write("| Load | Outcome | Token |\n| --- | --- | --- |\n")
        for name in ("first", "retry"):
            step = result[name]
            summary.write(f"| {name} | {step.get('outcome')} | {step.get('outputs', {}).get('token', '')} |\n")
        summary.write("\nProbe failures are observations; they are not hidden by the job conclusion.\n")
    if environment == "hosted-container" and result["classification"] != "current-at-both-loads":
        raise RuntimeError("The hosted control did not load the current version twice")


if __name__ == "__main__":
    main()
