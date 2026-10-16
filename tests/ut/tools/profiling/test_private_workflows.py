"""Private PR validation must use the requested code and immutable images."""

import importlib.util
import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from tools.profiling.capture import ProfileSpec

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
PR_REF = "${{ inputs.vllm_ascend_ref || github.sha }}"


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8-sig"), Loader=yaml.BaseLoader)


def test_only_explicit_triggers_remain():
    for path in WORKFLOWS.glob("*.y*ml"):
        events = workflow(path.name)["on"]
        assert not {"schedule", "push", "pull_request_target", "workflow_run"}.intersection(events)
        if "pull_request" in events:
            assert path.name == "schedule_nightly_image_build_with_base.yaml"
            assert events["pull_request"] == {"branches": ["main"], "types": ["labeled"]}
        if "issue_comment" in events:
            assert path.name == "slash_command_dispatch.yml"
    assert "repository_dispatch" not in workflow("schedule_daily_test.yaml")["on"]


def test_image_build_chain_preserves_private_publish_gates():
    entry = workflow("schedule_nightly_image_build_with_base.yaml")["jobs"]
    assert entry["setup-matrix"]["if"] == (
        "${{ github.event_name != 'pull_request' || github.event.label.name == 'nightly-image-build' }}"
    )
    build = entry["build-image"]
    assert build["uses"] == "./.github/workflows/_schedule_nightly_pair_image_build.yaml"
    assert build["with"]["should_push"] == (
        "${{ github.repository == 'vllm-project/vllm-ascend' && github.event_name != 'pull_request' }}"
    )
    assert build["with"]["skip_base"] == "${{ github.event_name == 'pull_request' }}"
    pair = workflow("_schedule_nightly_pair_image_build.yaml")["jobs"]
    assert pair["build-base"]["if"] == ("${{ !inputs.skip_base && github.repository == 'vllm-project/vllm-ascend' }}")
    assert pair["build-nightly"]["needs"] == "build-base"
    assert pair["build-nightly"]["with"]["os_filter"] == "${{ inputs.os }}"
    assert pair["build-nightly"]["with"]["base_image_branch"] == (
        "${{ inputs.base_image_branch || inputs.vllm_ascend_branch }}"
    )


@pytest.mark.parametrize(
    "build_type,os_filter,expected_os",
    [
        ("release", "all", ["ubuntu"]),
        ("release", "ubuntu", ["ubuntu"]),
        ("daily", "all", ["ubuntu", "openeuler", "ubuntu", "openeuler"]),
        ("daily", "ubuntu", ["ubuntu", "ubuntu"]),
        ("daily", "openeuler", ["openeuler", "openeuler"]),
    ],
)
def test_nightly_image_matrix_filters_os(build_type, os_filter, expected_os, tmp_path):
    jobs = workflow("_nightly_image_build.yaml")["jobs"]
    script = jobs["generate-matrix"]["steps"][0]["run"]
    script = script.replace("${{ inputs.build_type }}", build_type).replace("${{ inputs.os_filter }}", os_filter)
    output = tmp_path / "github-output"
    subprocess.run(["bash", "-eu", "-c", script], env={**os.environ, "GITHUB_OUTPUT": str(output)}, check=True)
    matrix = json.loads(output.read_text().removeprefix("matrix="))
    assert [item["os"] for item in matrix["include"]] == expected_os


@pytest.mark.parametrize("soc", ["a2", "a3", "a3_560t"])
def test_capture_and_parser_share_pinned_image_and_pr_ref(soc):
    jobs = workflow(f"schedule_nightly_test_{soc}.yaml")["jobs"]
    images = set()
    for job in jobs.values():
        if job.get("uses", "").endswith(
            (
                "_e2e_profile_parse.yaml",
                "_e2e_nightly_single_node.yaml",
                "_e2e_nightly_single_node_560t.yaml",
                "_e2e_nightly_multi_node.yaml",
            )
        ):
            images.add(job["with"]["image"])
            if job["uses"].endswith("_e2e_profile_parse.yaml"):
                assert job["with"]["ref"] == PR_REF
    assert len(images) == 1
    assert "@sha256:" in images.pop()
    steps = jobs["setup-vars"]["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout profiling tools")
    assert checkout["with"]["ref"] == PR_REF


def test_multi_node_uses_private_pr_and_isolated_scripts():
    assert workflow("schedule_nightly_test_a3.yaml")["env"]["ASCEND_LOG_PREFIX"] == (
        "/root/.cache/ascend-logs/${{ github.run_id }}/${{ github.run_attempt }}"
    )
    data = workflow("_e2e_nightly_multi_node.yaml")
    inputs = data["on"]["workflow_call"]["inputs"]
    assert inputs["vllm_ascend_remote_url"]["default"] == "https://github.com/nv-action/vllm-benchmarks.git"
    steps = next(iter(data["jobs"].values()))["steps"]
    checkout = next(step for step in steps if step.get("name") == "Checkout code")
    assert checkout["with"]["ref"] == PR_REF
    template = (ROOT / "tests/e2e/common/multi_node/lws.yaml.jinja2").read_text()
    assert template.count('bash "$LOG_PREFIX/validation-source/run.sh"') == 2
    assert "/root/.cache/tests/run.sh" not in template
    script = (ROOT / "tests/e2e/common/multi_node/run.sh").read_text()
    assert 'git fetch --depth 1 origin "$VLLM_ASCEND_REF"' in script
    assert "verify_profile_source.py" in script


def test_external_validation_preserves_warmup_and_profiles_halfway():
    config = yaml.safe_load(
        (ROOT / "tests/e2e/cases/models/configs/DeepSeek/DeepSeek-V4.1-Flash-w8a8-PD-prefix.yaml").read_text()
    )
    assert config["dp_load_balancing"] == "external"
    assert (config["num_nodes"], config["npu_per_node"]) == (2, 16)
    benchmarks = config["benchmarks"]
    assert list(benchmarks) == ["perf_TPOT50_128k_1_prefix90_warmup", "perf_TPOT50_16k_1"]
    assert benchmarks["perf_TPOT50_128k_1_prefix90_warmup"]["num_prompts"] == 4
    perf = benchmarks["perf_TPOT50_16k_1"]
    assert (perf["num_prompts"], perf["batch_size"], perf["request_rate"]) == (192, 64, 2.7)
    spec = ProfileSpec.from_options(
        {"enabled": True, "start_after_requests": perf["num_prompts"] // 2, "benchmark_names": "perf_TPOT50_16k_1"}
    )
    assert spec.start_after_requests == 96
    assert "ASCEND_PROFILE_START_AFTER" not in spec.environment()
    assert spec.includes_benchmark("perf_TPOT50_16k_1", "performance")
    assert not spec.includes_benchmark("perf_TPOT50_128k_1_prefix90_warmup", "performance")


def test_pr_dispatch_preserves_base_branch_and_request_count_option():
    script = (WORKFLOWS / "pr_nightly_command.yml").read_text()
    assert script.count("--ref '${{ needs.authorize.outputs.pr_base || 'main' }}'") == 9
    assert "--profile-start-after-requests" in script


@pytest.mark.parametrize(
    "name",
    [
        "_e2e_nightly_multi_node.yaml",
        "_e2e_nightly_single_node.yaml",
        "_e2e_nightly_single_node_560t.yaml",
        "_e2e_profile_parse.yaml",
    ],
)
def test_profile_index_upload_uses_published_artifact_key(name):
    steps = [step for job in workflow(name)["jobs"].values() for step in job.get("steps", [])]
    package = next(step for step in steps if step.get("id") == "profile_bundle")
    assert '--github-output "$GITHUB_OUTPUT"' in package["run"]
    upload = next(step for step in steps if step.get("name") == "Upload profiling index")
    assert upload["if"] == "${{ always() && steps.profile_bundle.outputs.artifact_key != '' }}"


@pytest.fixture
def verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_profile_source", WORKFLOWS / "scripts/verify_profile_source.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_verify_source_accepts_pr_checkout(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier.subprocess, "check_output", lambda *args, **kwargs: "pr-sha\n")
    monkeypatch.setattr(verifier.importlib.util, "find_spec", lambda name: SimpleNamespace(origin=str(tmp_path / name)))
    verifier.verify_source(tmp_path, "pr-sha")


def test_verify_source_rejects_old_checkout(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier.subprocess, "check_output", lambda *args, **kwargs: "image-sha\n")
    with pytest.raises(RuntimeError, match="PR SHA"):
        verifier.verify_source(tmp_path, "pr-sha")


def test_verify_source_rejects_image_import(verifier, monkeypatch, tmp_path):
    monkeypatch.setattr(verifier.subprocess, "check_output", lambda *args, **kwargs: "pr-sha\n")
    monkeypatch.setattr(verifier.importlib.util, "find_spec", lambda name: SimpleNamespace(origin="/image/tools.py"))
    with pytest.raises(RuntimeError, match="requested checkout"):
        verifier.verify_source(tmp_path, "pr-sha")


def test_parser_checks_tools_without_importing_runtime(verifier, monkeypatch, tmp_path):
    checked = []
    monkeypatch.setattr(verifier.subprocess, "check_output", lambda *args, **kwargs: "pr-sha\n")

    def find_spec(name):
        checked.append(name)
        return SimpleNamespace(origin=str(tmp_path / name))

    monkeypatch.setattr(verifier.importlib.util, "find_spec", find_spec)
    verifier.verify_source(tmp_path, "pr-sha", tools_only=True)
    assert checked == ["tools.profiling", "tools.aisbench"]
