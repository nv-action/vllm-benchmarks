"""Contract tests for normalizing the complete pyperformance default suite."""

import json
import os
import subprocess
import sys
from pathlib import Path


PARSER = Path(__file__).resolve().parents[1] / "scripts" / "parse_benchmark.py"


def test_normalizes_all_official_benchmarks_without_a_fixed_name_list(tmp_path):
    official = tmp_path / "benchmark.json"
    normalized = tmp_path / "benchmark_python.json"
    official.write_text(
        json.dumps(
            {
                "version": "1.0",
                "benchmarks": [
                    {
                        "metadata": {"name": name, "unit": "second"},
                        "runs": [{"values": values}],
                    }
                    for name, values in (
                        ("2to3", [1.0, 2.0, 3.0]),
                        ("json_dumps", [0.1, 0.2, 0.3]),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "SOFTWARE_VERSION": "3.14.7",
        "EXPECTED_ARCH": "x86_64",
        "PYPERFORMANCE_VERSION": "1.13.0",
        "PYPERFORMANCE_WARMUP": "3",
        "CONFIGURE_OPTIONS": "--enable-optimizations --with-lto",
    }
    completed = subprocess.run(
        [sys.executable, str(PARSER), str(official), str(normalized)],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(normalized.read_text(encoding="utf-8"))
    assert list(result["results"]) == ["2to3", "json_dumps"]
    assert result["results"]["json_dumps"]["value"] == 0.2
    assert result["parameters"]["benchmark_group"] == "default"
    assert "-b" not in result["parameters"]["command"]
