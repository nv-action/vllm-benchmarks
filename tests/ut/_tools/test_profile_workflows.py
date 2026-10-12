from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def load_workflow(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


@pytest.mark.parametrize(
    ("soc", "producers"),
    [
        ("a2", ("multi-node", "single-node")),
        ("a3", ("multi-node", "double-node", "single-node", "multi-card")),
        ("a3_560t", ("single-node", "multi-card")),
        ("310p", ("single-node",)),
        ("a5", ("multi-node", "single-node")),
    ],
)
def test_nightly_profiling_jobs(soc, producers):
    workflow = load_workflow(f"schedule_nightly_test_{soc}.yaml")
    assert workflow["on"]["workflow_dispatch"]["inputs"]["profile_options_json"]["default"] == "{}"

    for stage in producers:
        producer = workflow["jobs"][f"{stage}-tests"]
        parser = workflow["jobs"][f"parse-{stage}-profiles"]
        assert producer["with"]["profile_options_json"] == "${{ needs.setup-vars.outputs.profile_options }}"
        assert "HW_OBS_PROFILE_AK" in producer["secrets"]
        assert "HW_OBS_PROFILE_SK" in producer["secrets"]
        assert parser["uses"] == "./.github/workflows/_e2e_profile_parse.yaml"
        assert f"{stage}-tests" in parser["needs"]
        assert "needs.setup-vars.outputs.profile_parse == 'true'" in parser["if"]
        assert parser["with"]["image"] == producer["with"]["image"]
        assert parser["with"]["ref"] == "${{ github.sha }}"
        assert parser["with"]["display_name"] == "${{ matrix.test_config.config_file_path || matrix.test_config.name }}"
        assert "matrix.vllm_ascend_branch" in parser["with"]["prefix"]
        assert "needs.parse-trigger.outputs.filter" in parser["with"]["should_run"]
        assert "runner" not in parser["with"]


@pytest.mark.parametrize(
    "name", ["_e2e_nightly_single_node.yaml", "_e2e_nightly_single_node_560t.yaml", "_e2e_nightly_multi_node.yaml"]
)
def test_producer_only_uploads_raw(name):
    workflow = load_workflow(name)
    job = next(iter(workflow["jobs"].values()))
    step = next(step for step in job["steps"] if "python3 -m tools.profiling.capture upload" in step.get("run", ""))
    script = step["run"]
    assert "python3 -m tools.profiling.storage status" in script
    assert "python3 -m tools.profiling.capture upload" in script
    assert "python3 -m tools.profiling.parse parse" not in script
    assert not step.get("continue-on-error", False)
    if "multi_node" not in name:
        assert "inputs.vllm_ascend_branch" in step["env"]["PROFILE_KEY_PREFIX"]


def test_parser_uses_one_runner_per_node_and_finalizes_manifest():
    workflow = load_workflow("_e2e_profile_parse.yaml")
    prepare = workflow["jobs"]["prepare"]
    parser = workflow["jobs"]["parse"]
    finalize = workflow["jobs"]["finalize"]
    for job in (prepare, parser, finalize):
        assert job["runs-on"] == "linux-arm64-cpu-16"
        assert job["container"]["image"] == "${{ inputs.image }}"

    assert prepare["if"] == "${{ inputs.should_run }}"
    assert prepare["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    plan_script = prepare["steps"][-1]["run"]
    assert "python3 -m tools.profiling.storage status" in plan_script
    assert "python3 -m tools.profiling.parse plan" in plan_script

    assert parser["strategy"]["matrix"] == "${{ fromJSON(needs.prepare.outputs.matrix) }}"
    parse_script = parser["steps"][-1]["run"]
    assert "python3 -m tools.profiling.parse parse" in parse_script
    assert "--node-index ${{ matrix.node_index }}" in parse_script
    assert "needs.prepare.outputs.has_work == 'true'" in parser["if"]

    finalize_script = finalize["steps"][-1]["run"]
    assert "python3 -m tools.profiling.parse finalize" in finalize_script


def test_pr_nightly_dispatch_forwards_profiling_to_all_socs():
    workflow = load_workflow("pr_nightly_command.yml")
    jobs = workflow["jobs"]
    assert jobs["authorize"]["outputs"]["profile_options_json"] == "${{ steps.resolve.outputs.profile_options_json }}"
    for soc in ("a2", "a3", "a3-560t", "310p", "a5"):
        job = jobs[f"dispatch-{soc}"]
        script = next(step["run"] for step in job["steps"] if step["name"].startswith("Dispatch nightly-"))
        assert '-f profile_options_json="$PROFILE_OPTIONS_JSON"' in script


@pytest.mark.parametrize(
    ("soc", "stage"), [("a2", "multi-node"), ("a3", "multi-node"), ("a3", "double-node"), ("a5", "multi-node")]
)
def test_profiling_clones_private_validation_branch(soc, stage):
    job = load_workflow(f"schedule_nightly_test_{soc}.yaml")["jobs"][f"{stage}-tests"]
    assert "https://github.com/nv-action/vllm-benchmarks.git" in job["with"]["vllm_ascend_remote_url"]
    assert "needs.setup-vars.outputs.profile_options" in job["with"]["vllm_ascend_remote_url"]
    assert "inputs.vllm_ascend_ref" in job["with"]["vllm_ascend_ref"]


@pytest.mark.parametrize("name", ["_e2e_nightly_single_node.yaml", "_e2e_nightly_single_node_560t.yaml"])
def test_private_image_overlay_is_limited_to_profiling_and_case_config(name):
    job = next(iter(load_workflow(name)["jobs"].values()))
    steps = job["steps"]
    overlay = next(step for step in steps if step["name"] == "Overlay profiling Python source on existing image")
    assert "fromJSON(inputs.profile_options_json).enabled" in overlay["if"]
    assert "inputs.request_id == ''" in overlay["if"]
    assert "tests/e2e/common/single_node/test_single_node.py" in overlay["run"]
    assert 'config_path="${CONFIG_BASE_PATH}/${CONFIG_YAML_PATH}"' in overlay["run"]
    assert "vllm_ascend/worker" not in overlay["run"]
    assert steps.index(overlay) < next(i for i, step in enumerate(steps) if step["name"] == "Configure profiling")


@pytest.mark.parametrize(
    "name",
    [
        "_e2e_profile_parse.yaml",
        "_e2e_nightly_single_node.yaml",
        "_e2e_nightly_single_node_560t.yaml",
        "_e2e_nightly_multi_node.yaml",
    ],
)
def test_obs_dependency_installation_uses_bounded_mirror_fallback(name):
    scripts = [
        step["run"]
        for job in load_workflow(name)["jobs"].values()
        for step in job.get("steps", [])
        if "run" in step and "import boto3" in step["run"]
    ]
    assert scripts
    for script in scripts:
        assert "Using boto3" in script
        assert "timeout 300s python3 -m pip install" in script
        assert "--index-url https://repo.huaweicloud.com/repository/pypi/simple" in script
        assert "--timeout 15 --retries 1 boto3" in script
        assert "pip install --quiet" not in script
