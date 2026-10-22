#!/usr/bin/env python3
"""Repair only the GCC 14.4.0 report metadata from a completed run attempt.

The original SPEC outputs and normalized results are preserved verbatim beside
the recovered normalized result. Measurements are never recalculated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def signature(parameters: dict) -> str:
    payload = {"software": "gcc", "version": "14.4.0", "parameters": parameters}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def recover(root: Path, source_run_id: str, target_run_id: str) -> None:
    expected = {"x86_64": "spec-gcc-x86.cfg", "aarch64": "spec-gcc-aarch64.cfg"}
    paths = sorted(root.rglob("normalized_result.json"))
    if len(paths) != 2:
        raise ValueError(f"expected exactly two GCC results, found {len(paths)}")
    seen = set()
    for path in paths:
        result = json.loads(path.read_text(encoding="utf-8"))
        arch = result.get("architecture")
        if arch not in expected or arch in seen:
            raise ValueError(f"unexpected or duplicate architecture: {arch}")
        seen.add(arch)
        if (result.get("category"), result.get("software"), result.get("version"), result.get("run_id")) != (
            "Toolchain", "gcc", "14.4.0", source_run_id
        ):
            raise ValueError(f"source identity mismatch: {path}")
        if result.get("status") != "passed" or result.get("cleanup_status") != "passed":
            raise ValueError(f"source result is not passed: {path}")
        original = path.read_bytes()
        source = json.loads((path.parent / "benchmark_gcc.json").read_text(encoding="utf-8"))
        parameters = result.get("parameters", {})
        command = ["runcpu", f"--config={expected[arch]}", "intrate", "-n", "1", "-C", "384"]
        old = {"benchmark_result": {"command": command, "suite": "SPEC CPU2017 intrate", "copies": 384, "iterations": 1}}
        if parameters != old or source.get("parameters") != old["benchmark_result"]:
            raise ValueError(f"unexpected SPEC workload parameters: {path}")
        if result.get("parameter_signature") != signature(old):
            raise ValueError(f"source parameter signature mismatch: {path}")
        if result.get("sources", {}).get("benchmark_result") != source:
            raise ValueError(f"source JSON differs from normalized source: {path}")
        if not result.get("metrics"):
            raise ValueError(f"missing measurements: {path}")

        new = {"benchmark_result": {key: value for key, value in old["benchmark_result"].items() if key != "command"}}
        result["run_id"] = target_run_id
        result["parameters"] = new
        result["parameter_signature"] = signature(new)
        result["recovery"] = {
            "source_run_id": source_run_id,
            "source_normalized_sha256": hashlib.sha256(original).hexdigest(),
            "reason": "Architecture-specific SPEC configuration filename is build context, not workload identity",
            "preserved_measurements": True,
        }
        (path.parent / "normalized_result.original.json").write_bytes(original)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"[recover-gcc] {arch}: preserved metrics and source output; aligned report metadata")
    if seen != set(expected):
        raise ValueError("missing architecture")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-root", type=Path, required=True)
    parser.add_argument("--source-run-id", required=True)
    parser.add_argument("--target-run-id", required=True)
    args = parser.parse_args()
    recover(args.input_root, args.source_run_id, args.target_run_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
