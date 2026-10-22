"""Checks for the glibc 2.31 public-function metric contract."""

import importlib.util
import json
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location(
    "glibc_parse_benchmark", Path(__file__).with_name("parse_benchmark.py")
)
assert SPEC is not None and SPEC.loader is not None
PARSER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PARSER)
RUNTIME_SPEC = importlib.util.spec_from_file_location(
    "glibc_standalone_runtime", Path(__file__).with_name("standalone_runtime.py")
)
assert RUNTIME_SPEC is not None and RUNTIME_SPEC.loader is not None
RUNTIME = importlib.util.module_from_spec(RUNTIME_SPEC)
RUNTIME_SPEC.loader.exec_module(RUNTIME)


def write_output(directory, function, *, timing_type="clock_gettime_ns", public=True):
    implementations = [f"generic_{function}"]
    if public:
        implementations.append(function)
    rows = []
    for length in (2048, 4096):
        row = {
            "length": length,
            "align1": 0,
            "align2": 0,
            "timings": [90.0, float(length // 1024)] if public else [90.0],
        }
        if function == "memcmp":
            row["expected_result"] = 0
        rows.append(row)
    (directory / f"bench-{function}.out").write_text(
        json.dumps(
            {
                "timing_type": timing_type,
                "functions": {function: {"ifuncs": implementations, "results": rows}},
            }
        ),
        encoding="utf-8",
    )


def test_public_metrics_use_same_scenarios_and_nanoseconds(tmp_path):
    write_output(tmp_path, "memcpy")
    write_output(tmp_path, "memcmp")

    metrics = PARSER.extract_metrics(tmp_path)

    assert len(metrics) == 4
    assert metrics["memcpy.length=2048.align1=0.align2=0"]["value"] == 2
    assert metrics["memcmp.length=4096.align1=0.align2=0.expected_result=0"]["value"] == 4
    assert {metric["raw_unit"] for metric in metrics.values()} == {"ns"}
    assert {metric["source_field"].split(".")[-1] for metric in metrics.values()} == {
        "timings[1]"
    }
    standalone_metrics = RUNTIME.extract_metrics(
        {"software": "glibc", "version": "2.31", "architecture": "x86_64", "results": metrics},
        "2.31",
        "x86_64",
    )
    assert set(standalone_metrics) == set(metrics)


@pytest.mark.parametrize("timing_type", ["hp_timing", "", "clock_gettime_cycles"])
def test_other_timing_backends_are_rejected(tmp_path, timing_type):
    write_output(tmp_path, "memcpy", timing_type=timing_type)
    write_output(tmp_path, "memcmp")
    with pytest.raises(ValueError, match="does not use clock_gettime_ns"):
        PARSER.extract_metrics(tmp_path)


def test_missing_public_function_is_rejected(tmp_path):
    write_output(tmp_path, "memcpy", public=False)
    write_output(tmp_path, "memcmp")
    with pytest.raises(ValueError, match="public memcpy entry"):
        PARSER.extract_metrics(tmp_path)


def test_memcpy_page_size_duplicate_uses_first_sweep_result(tmp_path):
    write_output(tmp_path, "memcpy")
    write_output(tmp_path, "memcmp")
    path = tmp_path / "bench-memcpy.out"
    payload = json.loads(path.read_text(encoding="utf-8"))
    duplicate = dict(payload["functions"]["memcpy"]["results"][1])
    duplicate["timings"] = [90.0, 999.0]
    payload["functions"]["memcpy"]["results"].append(duplicate)
    path.write_text(json.dumps(payload), encoding="utf-8")

    metric = PARSER.extract_metrics(tmp_path)["memcpy.length=4096.align1=0.align2=0"]
    assert metric["value"] == 4.0
    assert metric["source_field"] == "functions.memcpy.results[1].timings[1]"


def test_memcmp_requires_equal_input_marker(tmp_path):
    write_output(tmp_path, "memcpy")
    write_output(tmp_path, "memcmp")
    path = tmp_path / "bench-memcmp.out"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["functions"]["memcmp"]["results"][0]["expected_result"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="has no"):
        PARSER.extract_metrics(tmp_path)
