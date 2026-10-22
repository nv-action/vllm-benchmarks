"""Verify normalization and cross-architecture direction semantics."""

import os
import re
import subprocess
import sys
from pathlib import Path

import collect_environment
from build_info import (
    BuildInfoError,
    actual_version_path,
    load_build_info,
    record_build_info,
    reset_build_info,
)
from context import RunContext
from generate_comparison import compare_pair, generate
from json_helper import atomic_write_json
from mark_cleanup import mark
from normalize_results import ResultValidationError, normalize, validate_stage_outputs
from prepare_result_history import prepare
from process_scanner import matching_processes, references_root
from reporting import render_comparison, render_single, render_summary


def normalized_result(architecture: str, higher: float, lower: float) -> dict:
    return {
        "category": "AI",
        "software": "sample",
        "version": "1.0",
        "architecture": architecture,
        "status": "passed",
        "cleanup_status": "passed",
        "build_info": {
            "requested_version": "1.0",
            "actual_version": "1.0",
            "recorded_at": "2026-08-17T00:00:00Z",
        },
        "system_info": {
            "architecture": architecture,
            "cpu_model": "Example CPU",
            "cpu_count": 64,
            "gcc_version": "13.2.1",
            "glibc_version": "glibc 2.38",
        },
        "runtime_before": {
            "memory": "before memory",
            "cpu_governor": "performance",
        },
        "runtime_after": {
            "memory": "after memory",
            "cpu_governor": "performance",
        },
        "parameters": {"benchmark.json": {"iterations": 3}},
        "parameter_signature": "same-parameters",
        "metrics": {
            "throughput": {"value": higher, "unit": "ops/s", "direction": "higher_is_better"},
            "latency": {"value": lower, "unit": "us", "direction": "lower_is_better"},
        },
    }


def sysbench_result(architecture: str, factor: float) -> dict:
    result = normalized_result(architecture, 100 * factor, 20 * factor)
    result.update({"category": "Database", "software": "mysql", "version": "8.0.46"})
    metrics = {}
    for scenario, base in (("sum", 1000), ("delete", 2000)):
        for threads in (128, 256):
            for field, unit, multiplier in (
                ("TPS", "transactions/s", 1),
                ("QPS", "queries/s", 2),
                ("transactions", "transactions", 60),
            ):
                name = f"metric {scenario}/{threads}/{field}"
                metrics[name] = {
                    "value": base * threads * multiplier * factor,
                    "unit": unit,
                    "direction": "higher_is_better",
                    "matrix": {
                        "group": scenario,
                        "row": threads,
                        "column": field,
                        "row_label": "线程数",
                        "column_order": ["TPS", "QPS", "transactions"],
                        "single_note": "TPS、QPS 和 transactions 均为越大越好。",
                    },
                }
    result["metrics"] = metrics
    return result


def record_requested_build(context: RunContext) -> dict:
    context.work_dir.mkdir(parents=True, exist_ok=True)
    actual_version_path(context).write_text(f"{context.version}\n", encoding="utf-8")
    record_build_info(context)
    return load_build_info(context)


def test_environment_collector_includes_gcc_and_glibc_versions(monkeypatch) -> None:
    responses = {
        ("gcc", "-dumpfullversion", "-dumpversion"): "13.2.1",
        ("getconf", "GNU_LIBC_VERSION"): "glibc 2.38",
    }
    monkeypatch.setattr(
        collect_environment,
        "_command",
        lambda args: responses.get(tuple(args), ""),
    )
    system_info = collect_environment.collect_system_info()
    runtime_state = collect_environment.collect_runtime_state()
    assert system_info["gcc_version"] == "13.2.1"
    assert system_info["glibc_version"] == "glibc 2.38"
    assert "cpu_count" in system_info
    assert "memory" not in system_info
    assert "gcc_version" not in runtime_state
    assert set(runtime_state) == {"collected_at", "memory", "cpu_governor"}


def test_cpu_model_keeps_complete_lscpu_model_name(monkeypatch) -> None:
    model_name = "Kunpeng 920 7270Z To be filled by O.E.M. CPU @ 2.9GHz"
    monkeypatch.setattr(
        collect_environment,
        "_command",
        lambda args: f"Architecture: aarch64\nModel name: {model_name}\n"
        if args == [
            "/usr/bin/sudo",
            "-n",
            "/usr/bin/env",
            "LC_ALL=C",
            "/usr/bin/lscpu",
        ]
        else "",
    )
    assert collect_environment._cpu_model() == model_name


def test_cpu_model_does_not_fall_back_when_lscpu_has_no_model_name(monkeypatch) -> None:
    monkeypatch.setattr(collect_environment, "_command", lambda _args: "")
    assert collect_environment._cpu_model() == "unknown"


def test_operating_system_uses_os_release_pretty_name(monkeypatch) -> None:
    monkeypatch.setattr(
        collect_environment.Path,
        "read_text",
        lambda *_args, **_kwargs: (
            'NAME="openEuler"\nPRETTY_NAME="openEuler 24.03 (LTS-SP4)"\n'
        ),
    )
    assert collect_environment._os_pretty_name() == "openEuler 24.03 (LTS-SP4)"


def test_operating_system_requires_os_release_pretty_name(monkeypatch) -> None:
    monkeypatch.setattr(
        collect_environment.Path,
        "read_text",
        lambda *_args, **_kwargs: 'NAME="openEuler"\n',
    )
    assert collect_environment._os_pretty_name() == "ERROR"


def test_operating_system_read_failure_returns_error(monkeypatch) -> None:
    def fail_read(*_args, **_kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(collect_environment.Path, "read_text", fail_read)
    assert collect_environment._os_pretty_name() == "ERROR"


def test_normalizer_extracts_metric_from_declared_output_name(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    atomic_write_json(
        output / "results.json",
        {"parameters": {"iterations": 3}, "summary": {"qps": 42.5}},
    )
    atomic_write_json(output / "system_info.json", {"cpu_count": 64})
    atomic_write_json(output / "runtime_before.json", {"cpu_governor": "performance"})
    atomic_write_json(output / "runtime_after.json", {"cpu_governor": "performance"})
    case = {
        "outputs": {
            "result": {
                "path": "results.json",
                "stage": "test",
                "format": "json",
                "required": True,
            }
        },
        "metrics": {
            "source": "result",
            "definitions": {
                "qps": {
                    "path": "summary.qps",
                    "unit": "queries/s",
                    "direction": "higher_is_better",
                }
            }
        },
    }
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case=case,
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=output,
        work_dir=tmp_path / "work",
    )
    build_info = record_requested_build(context)
    result = normalize(context, "passed")
    assert result["build_info"] == build_info
    assert result["system_info"] == {"cpu_count": 64}
    assert result["runtime_before"] == {"cpu_governor": "performance"}
    assert result["runtime_after"] == {"cpu_governor": "performance"}
    assert "environment_before" not in result
    assert "environment_after" not in result
    assert result["metrics"]["qps"]["value"] == 42.5
    assert result["metrics"]["qps"]["direction"] == "higher_is_better"
    assert result["parameters"] == {"result": {"iterations": 3}}
    assert result["sources"]["result"]["summary"]["qps"] == 42.5
    assert len(result["parameter_signature"]) == 64


def test_normalizer_extracts_every_metric_from_a_declared_collection(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    atomic_write_json(
        output / "results.json",
        {
            "results": {
                "scenario_00": {"name": "compress", "speed": 304.9},
                "scenario_01": {"name": "decompress", "speed": 1137.6},
                "scenario_02": {"name": "compress_freshCCtx", "speed": 308.0},
            }
        },
    )
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={
            "execution": {},
            "outputs": {
                "result": {
                    "path": "results.json",
                    "stage": "test",
                    "format": "json",
                    "required": True,
                }
            },
            "metrics": {
                "source": "result",
                "collection": {
                    "path": "results",
                    "name_path": "name",
                    "value_path": "speed",
                    "unit": "MB/s",
                    "direction": "higher_is_better",
                },
            },
        },
        category="HPC",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=output,
        work_dir=tmp_path / "work",
    )
    record_requested_build(context)
    result = normalize(context, "passed")
    assert list(result["metrics"]) == [
        "compress",
        "decompress",
        "compress_freshCCtx",
    ]
    assert result["metrics"]["compress"] == {
        "value": 304.9,
        "unit": "MB/s",
        "direction": "higher_is_better",
    }
    atomic_write_json(
        output / "results.json",
        {"results": {"scenario_00": {"name": "compress", "speed": "fast"}}},
    )
    import pytest

    with pytest.raises(ResultValidationError, match="must be numeric"):
        normalize(context, "passed")


def test_normalizer_collection_reads_unit_and_direction_from_each_item(
    tmp_path: Path,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    atomic_write_json(
        output / "results.json",
        {
            "results": {
                "throughput": {
                    "source_name": "serialize_qps",
                    "value": 304.9,
                    "unit": "messages/s",
                    "direction": "higher_is_better",
                    "group": "吞吐",
                },
                "latency": {
                    "source_name": "serialize_latency_us",
                    "value": 2.5,
                    "unit": "us",
                    "direction": "lower_is_better",
                    "group": "延迟",
                },
            }
        },
    )
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={
            "execution": {},
            "outputs": {
                "result": {
                    "path": "results.json",
                    "stage": "test",
                    "format": "json",
                    "required": True,
                }
            },
            "metrics": {
                "source": "result",
                "collection": {
                    "path": "results",
                    "name_path": "source_name",
                    "value_path": "value",
                    "unit_path": "unit",
                    "direction_path": "direction",
                    "group_path": "group",
                },
            },
        },
        category="HPC",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=output,
        work_dir=tmp_path / "work",
    )
    record_requested_build(context)
    result = normalize(context, "passed")
    assert result["metrics"] == {
        "serialize_qps": {
            "value": 304.9,
            "unit": "messages/s",
            "direction": "higher_is_better",
            "group": "吞吐",
        },
        "serialize_latency_us": {
            "value": 2.5,
            "unit": "us",
            "direction": "lower_is_better",
            "group": "延迟",
        },
    }
def test_normalizer_rejects_missing_empty_and_non_numeric_metrics(tmp_path: Path) -> None:
    import pytest

    output = tmp_path / "output"
    output.mkdir()
    case = {
        "execution": {},
        "outputs": {
            "result": {
                "path": "results.json",
                "stage": "test",
                "format": "json",
                "required": True,
            }
        },
        "metrics": {
            "source": "result",
            "definitions": {
                "qps": {
                    "path": "summary.qps",
                    "unit": "queries/s",
                    "direction": "higher_is_better",
                }
            }
        },
    }
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case=case,
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=output,
        work_dir=tmp_path / "work",
    )
    record_requested_build(context)

    (output / "results.json").write_text("", encoding="utf-8")
    with pytest.raises(ResultValidationError, match="empty"):
        normalize(context, "passed")
    atomic_write_json(output / "results.json", {"summary": {}})
    with pytest.raises(ResultValidationError, match="path does not exist"):
        normalize(context, "passed")
    atomic_write_json(output / "results.json", {"summary": {"qps": "fast"}})
    with pytest.raises(ResultValidationError, match="must be numeric"):
        normalize(context, "passed")
    (output / "results.json").write_text(
        '{"summary":{"qps":NaN}}\n', encoding="utf-8"
    )
    with pytest.raises(ResultValidationError, match="must be finite"):
        normalize(context, "passed")


def test_metric_can_override_the_default_logical_output_source(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    atomic_write_json(output / "primary.json", {"summary": {"qps": 42}})
    atomic_write_json(output / "latency.json", {"summary": {"latency": 1.5}})
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={
            "execution": {},
            "outputs": {
                "primary": {
                    "path": "primary.json",
                    "stage": "test",
                    "format": "json",
                    "required": True,
                },
                "latency_result": {
                    "path": "latency.json",
                    "stage": "test",
                    "format": "json",
                    "required": True,
                },
            },
            "metrics": {
                "source": "primary",
                "definitions": {
                    "qps": {
                        "path": "summary.qps",
                        "unit": "ops/s",
                        "direction": "higher_is_better",
                    },
                    "latency": {
                        "source": "latency_result",
                        "path": "summary.latency",
                        "unit": "ms",
                        "direction": "lower_is_better",
                    },
                },
            },
        },
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=output,
        work_dir=tmp_path / "work",
    )
    record_requested_build(context)
    result = normalize(context, "passed")
    assert result["metrics"]["qps"]["value"] == 42
    assert result["metrics"]["latency"]["value"] == 1.5


def test_framework_owns_and_strictly_validates_build_info(tmp_path: Path) -> None:
    import pytest

    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={},
        category="AI",
        software="sample",
        version="1.0",
        architecture="aarch64",
        run_id="unit-run",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    context.output_dir.mkdir()
    context.work_dir.mkdir()

    with pytest.raises(BuildInfoError, match="did not report"):
        record_build_info(context)

    actual_version_path(context).write_text("2.0\n", encoding="utf-8")
    with pytest.raises(BuildInfoError, match="requested version 1.0, built version 2.0"):
        record_build_info(context)

    actual_version_path(context).write_text("1.0\n", encoding="utf-8")
    path = record_build_info(context)
    payload = load_build_info(context)
    assert path.name == "build_info.json"
    assert payload["software"] == "sample"
    assert payload["requested_version"] == "1.0"
    assert payload["actual_version"] == "1.0"
    assert payload["architecture"] == "aarch64"
    assert "cpu_model" not in payload

    payload["cpu_model"] = "software-owned-value"
    atomic_write_json(path, payload)
    with pytest.raises(BuildInfoError, match="fields must be exactly"):
        load_build_info(context)

    del payload["cpu_model"]
    payload["actual_version"] = "2.0"
    atomic_write_json(path, payload)
    with pytest.raises(BuildInfoError, match="field actual_version"):
        load_build_info(context)

    reset_build_info(context)
    assert not path.exists()
    assert not actual_version_path(context).exists()


def test_stage_output_validation_checks_only_the_completed_stage(tmp_path: Path) -> None:
    import pytest

    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={
            "execution": {},
            "outputs": {
                "version": {
                    "path": "version.json",
                    "stage": "build",
                    "format": "json",
                    "required": True,
                },
                "result": {
                    "path": "results.json",
                    "stage": "test",
                    "format": "json",
                    "required": True,
                },
            },
            "metrics": {},
        },
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    context.output_dir.mkdir()
    atomic_write_json(context.output_dir / "version.json", {"version": "1.0"})
    validate_stage_outputs(context, "build")
    with pytest.raises(ResultValidationError, match="required output result is missing"):
        validate_stage_outputs(context, "test")


def test_stage_output_validation_accepts_a_nonempty_declared_directory(tmp_path: Path) -> None:
    context = RunContext(
        root=tmp_path,
        case_path=tmp_path / "case.yaml",
        case={
            "execution": {},
            "outputs": {
                "raw_evidence": {
                    "path": "evidence",
                    "stage": "test",
                    "format": "directory",
                    "required": True,
                }
            },
            "metrics": {},
        },
        category="AI",
        software="sample",
        version="1.0",
        architecture="x86_64",
        run_id="unit-run",
        output_dir=tmp_path / "output",
        work_dir=tmp_path / "work",
    )
    (context.output_dir / "evidence").mkdir(parents=True)
    (context.output_dir / "evidence" / "raw.txt").write_text("raw\n", encoding="utf-8")

    validate_stage_outputs(context, "test")


def test_comparison_inverts_lower_is_better_and_explains_direction() -> None:
    comparison = compare_pair(
        normalized_result("x86_64", higher=100, lower=20),
        normalized_result("aarch64", higher=120, lower=10),
    )
    assert comparison["metrics"]["throughput"]["relative_performance"] == 1.2
    assert comparison["metrics"]["latency"]["relative_performance"] == 2.0
    markdown = render_comparison(comparison)
    assert "越大越好" in markdown
    assert "越小越好" in markdown
    assert "## 测试环境" in markdown
    environment = markdown.split("## 测试环境", 1)[1]
    assert "### aarch64" not in environment
    assert "### x86_64" not in environment
    assert "### 构建信息" in environment
    assert "### 系统信息" in environment
    assert environment.count('<table width="1380">') == 2
    assert environment.count('<th width="180">项目</th>') == 2
    assert environment.count('<th width="600">x86_64</th>') == 2
    assert environment.count('<th width="600">aarch64</th>') == 2
    assert '<td width="180">CPU 型号</td>' in environment
    assert '<td width="600">Example CPU</td>' in environment
    assert markdown.count('<table width="1380">') == 3
    assert "|---" not in markdown
    assert "运行状态" not in environment
    assert "before memory" not in environment
    assert "after memory" not in environment
    assert "Example CPU" in markdown
    assert "13.2.1" in markdown
    assert "glibc 2.38" in markdown


def test_explicit_metric_groups_render_as_separate_tables() -> None:
    x86 = normalized_result("x86_64", higher=100, lower=20)
    arm = normalized_result("aarch64", higher=120, lower=10)
    for result in (x86, arm):
        result["metrics"]["throughput"]["group"] = "吞吐"
        result["metrics"]["latency"]["group"] = "延迟"

    comparison = compare_pair(x86, arm)
    assert comparison["metrics"]["throughput"]["group"] == "吞吐"

    single = render_single(x86)
    assert "### 吞吐" in single
    assert "### 延迟" in single

    cross_architecture = render_comparison(comparison)
    assert "## 吞吐" in cross_architecture
    assert "## 延迟" in cross_architecture
    assert "ARM/x86 原始比值" not in cross_architecture
    assert "相对性能" in cross_architecture
    assert '<th width="400">指标</th>' in cross_architecture
    assert '<th width="380">相对性能</th>' in cross_architecture
    assert "aarch64 相对性能" not in cross_architecture

    summary = render_summary(
        {"total": 2, "passed": 2, "failed": 0, "comparisons": 1, "items": [x86, arm]},
        [comparison],
    )
    assert "##### 吞吐" in summary
    assert "##### 延迟" in summary


def test_single_report_includes_build_and_system_environment() -> None:
    markdown = render_single(normalized_result("aarch64", 120, 10))
    assert "## 测试环境" in markdown
    assert "### 构建信息" in markdown
    assert "### 系统信息" in markdown
    assert markdown.count('<table width="1380">') == 3
    assert markdown.count('<th width="180">项目</th>') == 2
    assert markdown.count('<th width="1200">aarch64</th>') == 2
    assert '<th width="1200">x86_64</th>' not in markdown
    assert "|---" not in markdown
    assert "实际软件版本" in markdown
    assert "Example CPU" in markdown
    assert "GCC 版本" in markdown
    assert "13.2.1" in markdown
    assert "glibc 2.38" in markdown
    assert "运行状态" not in markdown
    assert "before memory" not in markdown and "after memory" not in markdown

    x86_markdown = render_single(normalized_result("x86_64", 100, 20))
    assert x86_markdown.count('<table width="1380">') == 3
    assert x86_markdown.count('<th width="1200">x86_64</th>') == 2
    assert '<th width="1200">aarch64</th>' not in x86_markdown


def test_report_lists_shared_test_tools_once() -> None:
    x86 = normalized_result("x86_64", 100, 20)
    arm = normalized_result("aarch64", 120, 10)
    tools = {
        "database_blue": {"version": "af475922"},
        "sysbench": {"version": "1.0.17", "revision": "d634bce"},
    }
    x86["test_tools"] = tools
    arm["test_tools"] = tools

    comparison = compare_pair(x86, arm)
    markdown = render_comparison(comparison)
    environment = markdown.split("## 测试环境", 1)[1]
    assert "### 测试工具" in environment
    assert "database_blue" in environment
    assert "1.0.17 (d634bce)" in environment
    assert environment.count('<table width="1380">') == 3


def test_comparison_rejects_incompatible_metric_contracts() -> None:
    import pytest

    x86 = normalized_result("x86_64", higher=100, lower=20)
    arm = normalized_result("aarch64", higher=120, lower=10)
    arm["metrics"]["throughput"]["unit"] = "requests/s"
    with pytest.raises(ValueError, match="units differ"):
        compare_pair(x86, arm)

    arm = normalized_result("aarch64", higher=120, lower=10)
    del arm["metrics"]["latency"]
    comparison = compare_pair(x86, arm)
    assert comparison["metrics"]["latency"]["x86_64"] == 20
    assert comparison["metrics"]["latency"]["aarch64"] is None
    assert comparison["metrics"]["latency"]["relative_performance"] is None


def test_comparison_rejects_different_resolved_workload_parameters() -> None:
    import pytest

    x86 = normalized_result("x86_64", higher=100, lower=20)
    arm = normalized_result("aarch64", higher=120, lower=10)
    arm["parameters"]["benchmark.json"]["iterations"] = 5
    with pytest.raises(ValueError, match="resolved workload parameters differ"):
        compare_pair(x86, arm)


def test_report_generator_pairs_architectures(tmp_path: Path) -> None:
    input_root = tmp_path / "artifacts"
    atomic_write_json(
        input_root / "x86" / "normalized_result.json",
        normalized_result("x86_64", 100, 20),
    )
    atomic_write_json(
        input_root / "arm" / "normalized_result.json",
        normalized_result("aarch64", 120, 10),
    )
    summary = generate(input_root, tmp_path / "report")
    assert summary["total"] == 2
    assert summary["comparisons"] == 1
    assert (tmp_path / "report" / "combined-report.md").is_file()
    assert (tmp_path / "report" / "junit.xml").is_file()
    combined = (tmp_path / "report" / "combined-report.md").read_text(encoding="utf-8")
    assert "越大越好" in combined
    assert "越小越好" in combined
    assert "## 测试环境" in combined
    assert not (tmp_path / "report" / "AI-sample-1.0.md").exists()
    environment = combined.split("## 测试环境", 1)[1].split("## 单架构指标", 1)[0]
    assert "### sample 1.0" in environment
    assert "#### 构建信息" in environment
    assert "#### 系统信息" in environment
    assert environment.count('<table width="1380">') == 2
    assert environment.count('<th width="180">项目</th>') == 2
    assert environment.count('<th width="600">x86_64</th>') == 2
    assert environment.count('<th width="600">aarch64</th>') == 2
    assert combined.count('<table width="1380">') == 6
    assert "|---" not in combined
    assert "运行状态" not in environment
    assert "Example CPU" in combined
    assert "13.2.1" in combined
    assert "glibc 2.38" in combined
    assert "before memory" not in combined
    assert "after memory" not in combined
    assert combined.index("### x86_64") < combined.index("### aarch64")
    x86_metrics = combined.split("### x86_64", 1)[1].split("### aarch64", 1)[0]
    arm_metrics = combined.split("### aarch64", 1)[1].split("## 跨架构指标", 1)[0]
    cross_architecture_metrics = combined.split("## 跨架构指标", 1)[1]
    assert "#### sample 1.0" in x86_metrics
    assert "#### sample 1.0" in arm_metrics
    assert '<th width="500">指标</th>' in x86_metrics
    assert '<th width="500">指标</th>' in arm_metrics
    assert '<th width="180">软件</th>' not in x86_metrics
    assert '<th width="180">软件</th>' not in arm_metrics
    assert "### sample 1.0" in cross_architecture_metrics
    assert '<th width="450">指标</th>' in cross_architecture_metrics
    assert '<th width="190">优化方向</th>' in cross_architecture_metrics
    assert '<th width="360">aarch64 相对性能</th>' in cross_architecture_metrics
    assert "软件（版本）" not in cross_architecture_metrics
    for section in (arm_metrics, x86_metrics, cross_architecture_metrics):
        assert section.index("throughput") < section.index("latency")


def test_single_architecture_metrics_are_visible_in_combined_report(tmp_path: Path) -> None:
    input_root = tmp_path / "artifacts"
    atomic_write_json(
        input_root / "arm" / "normalized_result.json",
        normalized_result("aarch64", 120, 10),
    )
    generate(input_root, tmp_path / "report")
    combined = (tmp_path / "report" / "combined-report.md").read_text(encoding="utf-8")
    assert "单架构指标" in combined
    assert "### aarch64" in combined
    assert "### x86_64" not in combined
    assert "120" in combined
    assert "越大越好" in combined


def test_sysbench_reports_group_metrics_by_workload_then_threads(tmp_path: Path) -> None:
    input_root = tmp_path / "artifacts"
    atomic_write_json(
        input_root / "x86" / "normalized_result.json",
        sysbench_result("x86_64", 1),
    )
    atomic_write_json(
        input_root / "arm" / "normalized_result.json",
        sysbench_result("aarch64", 1.1),
    )
    generate(input_root, tmp_path / "report")

    combined = (tmp_path / "report" / "combined-report.md").read_text(encoding="utf-8")
    x86_metrics = combined.split("### x86_64", 1)[1].split("### aarch64", 1)[0]
    sum_section = x86_metrics.split("##### sum", 1)[1].split("##### delete", 1)[0]
    assert '<td width="180">128</td>' in sum_section
    assert '<td width="180">256</td>' in sum_section
    assert sum_section.index('<td width="180">128</td>') < sum_section.index(
        '<td width="180">256</td>'
    )
    assert "metric sum/128/TPS" not in sum_section

    cross_architecture = combined.split("## 跨架构指标", 1)[1]
    assert cross_architecture.index("#### sum") < cross_architecture.index("#### delete")
    assert "线程数" in cross_architecture


def test_permanent_history_keeps_compact_results_and_updates_dual_arch_baseline(
    tmp_path: Path,
) -> None:
    input_root = tmp_path / "artifacts"
    report_dir = tmp_path / "report"
    output_root = tmp_path / "permanent"
    run_id = "12345-1"
    for folder, architecture, throughput, latency in (
        ("x86", "x86_64", 100, 20),
        ("arm", "aarch64", 120, 10),
    ):
        result = normalized_result(architecture, throughput, latency)
        result.update({"run_id": run_id, "cleanup_status": "passed"})
        result_dir = input_root / folder
        result["sources"] = {
            "raw_evidence": {"path": "evidence/original-output.txt", "size": 12},
            "raw_benchmark_outputs": {"path": "benchtests", "file_count": 2},
        }
        atomic_write_json(result_dir / "normalized_result.json", result)
        atomic_write_json(result_dir / "benchmark.json", {"throughput": throughput})
        (result_dir / "evidence").mkdir(parents=True)
        (result_dir / "evidence" / "original-output.txt").write_text(
            f"source evidence for {architecture}\n", encoding="utf-8"
        )
        (result_dir / "benchtests" / "nested").mkdir(parents=True)
        (result_dir / "benchtests" / "bench-alpha.out").write_text(
            f"official output for {architecture}\n", encoding="utf-8"
        )
        (result_dir / "benchtests" / "nested" / "bench-beta.out").write_text(
            f"nested official output for {architecture}\n", encoding="utf-8"
        )
        (result_dir / "report.md").write_text(f"# {architecture}\n", encoding="utf-8")
        (result_dir / "raw-output.log").write_text(
            f"raw benchmark output for {architecture}\n", encoding="utf-8"
        )
        (result_dir / "results.log").write_text("large log\n", encoding="utf-8")
        (result_dir / "results.txt").write_text("duplicate summary\n", encoding="utf-8")

    generate(input_root, report_dir)
    prepared = prepare(
        input_root,
        report_dir,
        output_root,
        run_id=run_id,
        repository="owner/repo",
        source_commit="abc123",
        workflow_url="https://github.example/actions/runs/12345",
        update_baseline=True,
    )
    run_roots = list((output_root / "AI" / "sample" / "1.0").glob(f"*_{run_id}"))
    assert len(run_roots) == 1
    run_root = run_roots[0]
    assert re.fullmatch(
        rf"\d{{4}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}}-\d{{2}}_{re.escape(run_id)}",
        run_root.name,
    )
    assert prepared == [run_root]
    assert (run_root / "x86_64" / "benchmark.json").is_file()
    assert (run_root / "aarch64" / "normalized_result.json").is_file()
    assert (run_root / "x86_64" / "raw-output.log").read_text(
        encoding="utf-8"
    ) == "raw benchmark output for x86_64\n"
    assert (run_root / "aarch64" / "raw-output.log").read_text(
        encoding="utf-8"
    ) == "raw benchmark output for aarch64\n"
    assert (run_root / "x86_64" / "evidence" / "original-output.txt").read_text(
        encoding="utf-8"
    ) == "source evidence for x86_64\n"
    assert (run_root / "aarch64" / "benchtests" / "bench-alpha.out").read_text(
        encoding="utf-8"
    ) == "official output for aarch64\n"
    assert (run_root / "x86_64" / "benchtests" / "nested" / "bench-beta.out").read_text(
        encoding="utf-8"
    ) == "nested official output for x86_64\n"
    assert not (run_root / "aarch64" / "results.log").exists()
    assert not (run_root / "aarch64" / "results.txt").exists()
    assert (run_root / "comparison.json").is_file()
    assert (run_root / "combined-report.md").is_file()
    assert not (run_root / "comparison.md").exists()
    permanent_report = (run_root / "combined-report.md").read_text(encoding="utf-8")
    workflow_report = (report_dir / "combined-report.md").read_text(encoding="utf-8")
    assert permanent_report == workflow_report
    assert permanent_report.count("# 性能测试汇总") == 1
    assert "# sample 1.0 性能报告" not in permanent_report
    assert "# sample 1.0 跨架构对比" not in permanent_report
    assert "跨架构对比" in permanent_report
    assert "相对性能" in permanent_report
    baseline = output_root / "AI" / "sample" / "1.0" / "baseline.json"
    assert baseline.is_file()
    assert '"run_id": "12345-1"' in baseline.read_text(encoding="utf-8")
    assert f'"result_path": "AI/sample/1.0/{run_root.name}"' in baseline.read_text(
        encoding="utf-8"
    )


def test_baseline_update_rejects_a_single_architecture(tmp_path: Path) -> None:
    input_root = tmp_path / "artifacts"
    result = normalized_result("aarch64", 120, 10)
    result.update({"run_id": "12345-1", "cleanup_status": "passed"})
    atomic_write_json(input_root / "arm" / "normalized_result.json", result)
    generate(input_root, tmp_path / "report")
    import pytest

    with pytest.raises(ValueError, match="requires both architectures"):
        prepare(
            input_root,
            tmp_path / "report",
            tmp_path / "permanent",
            run_id="12345-1",
            repository="owner/repo",
            source_commit="abc123",
            workflow_url="https://github.example/actions/runs/12345",
            update_baseline=True,
        )


def test_permanent_history_rejects_failed_cleanup(tmp_path: Path) -> None:
    input_root = tmp_path / "artifacts"
    result = normalized_result("aarch64", 120, 10)
    result.update({"run_id": "12345-1", "cleanup_status": "failed"})
    atomic_write_json(input_root / "arm" / "normalized_result.json", result)

    import pytest

    with pytest.raises(ValueError, match="requires passed test and cleanup"):
        prepare(
            input_root,
            tmp_path / "report",
            tmp_path / "permanent",
            run_id="12345-1",
            repository="owner/repo",
            source_commit="abc123",
            workflow_url="https://github.example/actions/runs/12345",
        )


def test_result_history_publisher_creates_an_independent_branch(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    remote = tmp_path / "remote.git"
    source = tmp_path / "prepared"
    repository.mkdir()
    source.mkdir()

    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=repository, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Test User"], cwd=repository, check=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=repository, check=True
    )
    (repository / "main-only.txt").write_text("main branch\n", encoding="utf-8")
    subprocess.run(["git", "add", "main-only.txt"], cwd=repository, check=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "remote", "add", "origin", str(remote)], cwd=repository, check=True
    )

    result_path = source / "Database" / "sample" / "1.0.0" / "12345-1"
    result_path.mkdir(parents=True)
    (source / "README.md").write_text("# Results\n", encoding="utf-8")
    (result_path / "manifest.json").write_text("{}\n", encoding="utf-8")
    publisher = Path(__file__).resolve().parents[1] / "publish_result_history.sh"
    environment = os.environ.copy()
    environment.update({
        "RUNNER_TEMP": str(tmp_path),
        "GITHUB_RUN_ID": "12345",
        "GITHUB_RUN_ATTEMPT": "1",
    })
    subprocess.run(
        ["bash", str(publisher), str(source), "performance-results"],
        cwd=repository,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    tree = subprocess.run(
        ["git", f"--git-dir={remote}", "ls-tree", "-r", "--name-only", "performance-results"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    assert "Database/sample/1.0.0/12345-1/manifest.json" in tree
    assert "README.md" in tree
    assert "main-only.txt" not in tree
    duplicate = subprocess.run(
        ["bash", str(publisher), str(source), "performance-results"],
        cwd=repository,
        env=environment,
        capture_output=True,
        text=True,
    )
    assert duplicate.returncode == 3
    assert "immutable run already exists" in duplicate.stderr


def test_cleanup_failure_is_persisted(tmp_path: Path) -> None:
    atomic_write_json(tmp_path / "status.json", {"status": "passed", "cleanup_status": "pending"})
    atomic_write_json(tmp_path / "normalized_result.json", {"status": "passed"})
    mark(tmp_path, "failure")
    import json

    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    normalized = json.loads((tmp_path / "normalized_result.json").read_text(encoding="utf-8"))
    assert status["exit_code"] == 80
    assert status["cleanup_status"] == "failed"
    assert normalized["failed_stage"] == "cleanup"


def test_report_includes_status_when_execution_has_no_normalized_result(tmp_path: Path) -> None:
    atomic_write_json(tmp_path / "artifacts" / "failed" / "status.json", {
        "category": "AI",
        "software": "sample",
        "version": "1.0",
        "architecture": "x86_64",
        "status": "failed",
        "failed_stage": "architecture_check",
    })
    summary = generate(tmp_path / "artifacts", tmp_path / "report")
    assert summary["total"] == 1
    assert summary["failed"] == 1


def test_process_scanner_does_not_match_its_own_root_argument(tmp_path: Path) -> None:
    work_root = tmp_path / "boostkit-perf"
    scanner = Path(__file__).resolve().parents[1] / "process_scanner.py"
    completed = subprocess.run(
        [sys.executable, str(scanner), "--root", str(work_root)],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout == ""
    assert references_root(
        b"python3\0worker.py\0/home/runner/boostkit-perf/case-work\0",
        Path("/home/runner/boostkit-perf"),
    )


def test_process_scanner_finds_real_work_root_process(tmp_path: Path) -> None:
    work_root = tmp_path / "boostkit-perf"
    process = subprocess.Popen([
        sys.executable,
        "-c",
        "import time; time.sleep(30)",
        str(work_root / "case-work"),
    ])
    try:
        assert process.pid in matching_processes(work_root)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_process_scanner_finds_isolated_process_without_root_in_command(tmp_path: Path) -> None:
    work_root = tmp_path / "boostkit-perf"
    environment = os.environ.copy()
    environment["PERF_PROCESS_TOKEN"] = "boostkit-perf:old-run:AI:sample:1.0:x86_64"
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        env=environment,
    )
    try:
        assert process.pid in matching_processes(work_root)
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_process_scanner_finds_process_with_work_root_as_cwd(tmp_path: Path) -> None:
    work_root = tmp_path / "boostkit-perf"
    work_root.mkdir()
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        cwd=work_root,
    )
    try:
        assert process.pid in matching_processes(work_root)
    finally:
        process.terminate()
        process.wait(timeout=5)
