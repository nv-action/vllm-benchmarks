"""Contract checks for the original sra_test output adapter."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_sra_benchmark.py"
SPEC = importlib.util.spec_from_file_location("run_sra_benchmark", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

OFFICIAL_OUTPUT = """
Index build+prepare time: 10.2 s
Loop 1/3:
  QPS_total(wall) = 111.0
==================================================
Final Summary (same nq per thread):
  build+prepare_time = 10.2 s
  recall = 0.975
  final_QPS(mid50 wall mean) = 1200.5
  QPS_from_avg_thread_time * threads = 1250.4
"""


def test_parse_official_summary() -> None:
    assert MODULE.parse_summary(OFFICIAL_OUTPUT) == {
        "build_time_s": 10.2,
        "recall": 0.975,
        "qps_wall": 1200.5,
        "qps_avg_thread": 1250.4,
    }


def test_missing_wall_qps_fails() -> None:
    with pytest.raises(RuntimeError, match="qps_wall"):
        MODULE.parse_summary(OFFICIAL_OUTPUT.replace("final_QPS", "other_QPS"))


def test_runs_original_binary_and_keeps_log(tmp_path: Path) -> None:
    binary = tmp_path / "hnsw_test"
    binary.write_text(
        "#!/usr/bin/env python3\nprint(" + repr(OFFICIAL_OUTPUT) + ")\n",
        encoding="utf-8",
    )
    binary.chmod(0o755)
    summary = MODULE.run_case(tmp_path, tmp_path, "hnsw", "sift-128-euclidean")
    assert summary["qps_wall"] == 1200.5
    assert "Final Summary" in (
        tmp_path / "sra-test-logs" / "hnsw_sift-128-euclidean.log"
    ).read_text(encoding="utf-8")


def test_reference_configuration_is_not_mutated(tmp_path: Path) -> None:
    path = tmp_path / "hnsw.config"
    content = "nloop = 5\nnum_threads = 32\nbatch_mode = false\nsave_or_load = save\n"
    path.write_text(content, encoding="utf-8")
    assert MODULE.read_config(path)["num_threads"] == "32"
    assert path.read_text(encoding="utf-8") == content


def test_loading_prebuilt_index_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "hnsw.config"
    path.write_text("batch_mode = false\nsave_or_load = load\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="fresh index"):
        MODULE.read_config(path)
