"""Checks for the pinned sra_test hnswlib result adapter and report."""

from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_sra_benchmark.py"
SPEC = importlib.util.spec_from_file_location("hnswlib_sra_benchmark", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
BENCHMARK = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BENCHMARK)
RUNTIME_PATH = SCRIPT.with_name("standalone_runtime.py")
RUNTIME_SPEC = importlib.util.spec_from_file_location("hnswlib_standalone_runtime", RUNTIME_PATH)
assert RUNTIME_SPEC is not None and RUNTIME_SPEC.loader is not None
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME)


def test_only_hnswlib_080_is_accepted(tmp_path: Path) -> None:
    script = SCRIPT.parents[1] / "hnswlib_test.sh"
    completed = subprocess.run(
        ["bash", str(script), "--version", "0.9.0", "--results-dir", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 10
    assert "only hnswlib 0.8.0 is supported" in completed.stdout


def test_original_output_fields_are_parsed() -> None:
    output = """
  build+prepare_time = 12.25 s
  recall = 0.975
  final_QPS(mid50 wall mean) = 1234.5
  QPS_from_avg_thread_time * threads = 1250.75
"""
    assert BENCHMARK.parse_summary(output) == {
        "build_time_s": 12.25,
        "recall": 0.975,
        "qps_wall": 1234.5,
        "qps_avg_thread": 1250.75,
    }


@pytest.mark.parametrize("invalid", ["recall = 1.5", "recall = nan", "recall = 0.5\n  recall = 0.6"])
def test_invalid_recall_is_rejected(invalid: str) -> None:
    output = (
        "build+prepare_time = 1 s\n"
        f"{invalid}\n"
        "final_QPS(mid50 wall mean) = 100\n"
        "QPS_from_avg_thread_time * threads = 100\n"
    )
    with pytest.raises(RuntimeError):
        BENCHMARK.parse_summary(output)


def test_original_configuration_requires_fresh_single_query_index(tmp_path: Path) -> None:
    config = tmp_path / "hnswlib_sift.config"
    config.write_text(
        "k_f = 24\nefs = 100\nbatch_mode = false\nsave_or_load = save\nindex_path = indexes/sift.hnswlib\n",
        encoding="utf-8",
    )
    values = BENCHMARK.read_config(config)
    assert values["k_f"] == "24"
    assert values["save_or_load"] == "save"
    config.write_text(config.read_text(encoding="utf-8").replace("= save", "= load"), encoding="utf-8")
    with pytest.raises(RuntimeError, match="fresh index"):
        BENCHMARK.read_config(config)


def test_original_command_uses_hnswlib_test_without_pin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], Path]] = []

    class FakeProcess:
        def __init__(self, command: list[str], **kwargs: object) -> None:
            calls.append((command, kwargs["cwd"]))
            self.stdout = [
                "build+prepare_time = 1 s\n",
                "recall = 0.9\n",
                "final_QPS(mid50 wall mean) = 100\n",
                "QPS_from_avg_thread_time * threads = 110\n",
            ]

        def wait(self) -> int:
            return 0

    monkeypatch.setattr(BENCHMARK.subprocess, "Popen", FakeProcess)
    result = BENCHMARK.run_case(tmp_path / "source", tmp_path / "results", "sift-128-euclidean")
    assert calls == [
        ([str(tmp_path / "source" / "hnswlib_test"), "hnswlib", "sift-128-euclidean", "--no-pin"], tmp_path / "source")
    ]
    assert result["qps_wall"] == 100


def test_standalone_report_preserves_dataset_group() -> None:
    name = "hnswlib/sift-128-euclidean/qps_wall"
    benchmark = {
        "software": "hnswlib",
        "version": "0.8.0",
        "architecture": "x86_64",
        "results": {
            name: {
                "source_name": name,
                "group": "sift-128-euclidean",
                "value": 100.0,
                "unit": "queries/s",
                "direction": "higher_is_better",
            }
        },
    }
    metrics = RUNTIME.extract_metrics(benchmark, "0.8.0", "x86_64")
    assert metrics[name]["group"] == "sift-128-euclidean"
    report = RUNTIME.render_report({"version": "0.8.0", "run_id": "test", "architecture": "x86_64", "status": "passed", "cleanup_status": "passed", "metrics": metrics})
    assert "### sift-128-euclidean" in report
