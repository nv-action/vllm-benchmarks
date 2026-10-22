"""Check the Sysbench result matrix without running the long database workload."""

import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "collect_sysbench.py"
SPEC = importlib.util.spec_from_file_location("collect_sysbench", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def make_runs(root: Path) -> Path:
    runs = root / "sysbench-runs"
    runs.mkdir()
    for scenario in ("read_only", "read_write", "write_only"):
        for threads in (1, 32, 128, 256):
            (runs / f"run-{scenario}-threads{threads}.log").write_text(
                "transactions: 120 (12.00 per sec.)\n"
                "queries: 240 (24.00 per sec.)\n"
                "Latency (ms):\n    95th percentile: 3.14\n",
                encoding="utf-8",
            )
    return runs


def test_complete_matrix(tmp_path: Path) -> None:
    results = MODULE.collect(make_runs(tmp_path))
    assert len(results) == 36
    assert results["sysbench read_only --threads=1: TPS"]["value"] == 12.0
    assert results["sysbench write_only --threads=256: P95 latency"]["direction"] == "lower_is_better"


def test_missing_run_rejected(tmp_path: Path) -> None:
    runs = make_runs(tmp_path)
    (runs / "run-read_only-threads1.log").unlink()
    with pytest.raises(ValueError, match="missing 1 Sysbench runs"):
        MODULE.collect(runs)


def test_missing_summary_rejected(tmp_path: Path) -> None:
    runs = make_runs(tmp_path)
    (runs / "run-read_only-threads1.log").write_text("transactions: 1 (1.0 per sec.)\n")
    with pytest.raises(ValueError, match="expected one QPS summary"):
        MODULE.collect(runs)
