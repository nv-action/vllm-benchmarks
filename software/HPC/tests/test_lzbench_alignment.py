"""Machine-specific paths must not change cross-architecture workload parameters."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


HPC_DIR = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("software", "script", "selector", "version", "result_label"),
    [
        ("lz4", "run_lzbench.py", "-elz4", "1.9.4", "lz4 1.9.4"),
        ("snappy", "run_benchmark.py", "-esnappy", "1.2.2", "snappy 1.2.2"),
        ("zstd", "run_lzbench.py", "-ezstd,5", "1.5.7", "zstd 1.5.7 -5"),
    ],
)
def test_lzbench_parameters_align_across_work_directories(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    software: str,
    script: str,
    selector: str,
    version: str,
    result_label: str,
) -> None:
    script_path = HPC_DIR / software / "scripts" / script
    spec = importlib.util.spec_from_file_location(f"{software}_benchmark", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def fake_run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        output = (
            f"{result_label} 970 MB/s 4630 MB/s 5 50.00 {command[-1]}\n"
            "[Params] cTime=20.0 dTime=20.0 chunkSize=4KB\n"
        )
        return subprocess.CompletedProcess(command, 0, output)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    monkeypatch.setattr(module, "SILESIA_SHA256", hashlib.sha256(b"0123456789").hexdigest())
    monkeypatch.setenv("SOFTWARE_VERSION", version)
    results = []
    for architecture in ("x86_64", "aarch64"):
        work_dir = tmp_path / architecture
        work_dir.mkdir()
        binary = work_dir / "lzbench"
        binary.touch(mode=0o755)
        corpus = work_dir / "silesia.tar"
        corpus.write_bytes(b"0123456789")
        normalized = work_dir / "benchmark.json"
        monkeypatch.setenv("EXPECTED_ARCH", architecture)
        monkeypatch.setattr(
            sys, "argv", [str(script_path), str(binary), str(corpus), str(work_dir / "raw.txt"), str(normalized)]
        )
        assert module.main() == 0
        results.append(json.loads(normalized.read_text(encoding="utf-8")))

    assert results[0]["parameters"] == results[1]["parameters"]
    assert results[0]["parameters"]["command"] == ["lzbench", selector, "-b4", "-t20u20", "silesia.tar"]
    assert results[0]["runtime_context"]["command"] != results[1]["runtime_context"]["command"]
