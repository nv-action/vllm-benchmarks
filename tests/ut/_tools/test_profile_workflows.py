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
        parser = workflow["jobs"][f"parse-{stage}-profile"]
        assert "inputs.profile_options_json" in producer["with"]["profile_enabled"]
        assert producer["with"]["profile_start_after"].endswith("'15' }}")
        assert producer["with"]["profile_duration"].endswith("'8' }}")
        assert producer["with"]["profile_output"].endswith("'parsed' }}")
        assert "OBS_ACCESS_KEY_ID" in producer["secrets"]
        assert parser["uses"] == "./.github/workflows/_e2e_profile_parse.yaml"
        assert f"{stage}-tests" in parser["needs"]
        assert ".enabled" in parser["if"]
        assert "== 'parsed'" in parser["if"]
        assert parser["with"]["image"] == producer["with"]["image"]
        assert parser["with"]["ref"] == "${{ github.sha }}"
        assert parser["with"]["display_name"] == "${{ matrix.test_config.config_file_path || matrix.test_config.name }}"
        assert "matrix.vllm_ascend_branch" in parser["with"]["prefix"]
        assert "needs.parse-trigger.outputs.filter" in parser["with"]["should_run"]
        assert parser["with"]["runner"] == "linux-arm64-cpu-32-hk"


@pytest.mark.parametrize(
    "name", ["_e2e_nightly_single_node.yaml", "_e2e_nightly_single_node_560t.yaml", "_e2e_nightly_multi_node.yaml"]
)
def test_producer_only_uploads_raw(name):
    workflow = load_workflow(name)
    job = next(iter(workflow["jobs"].values()))
    step = next(step for step in job["steps"] if "python3 -m tools.profiling.workflow upload" in step.get("run", ""))
    script = step["run"]
    assert "python3 -m tools.profiling.workflow storage" in script
    assert "python3 -m tools.profiling.workflow upload" in script
    assert "python3 -m tools.profiling.workflow parse" not in script
    assert not step.get("continue-on-error", False)
    if "multi_node" not in name:
        assert "inputs.vllm_ascend_branch" in step["env"]["PROFILE_KEY_PREFIX"]


def test_parser_uses_one_runner_per_node_and_finalizes_manifest():
    workflow = load_workflow("_e2e_profile_parse.yaml")
    prepare = workflow["jobs"]["prepare"]
    parser = workflow["jobs"]["parse"]
    finalize = workflow["jobs"]["finalize"]
    for job in (prepare, parser, finalize):
        assert job["runs-on"] == "${{ inputs.runner }}"
        assert job["container"]["image"] == "${{ inputs.image }}"

    assert prepare["if"] == "${{ inputs.should_run }}"
    assert prepare["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    plan_script = prepare["steps"][-1]["run"]
    assert "python3 -m tools.profiling.workflow storage" in plan_script
    assert "python3 -m tools.profiling.workflow plan" in plan_script

    assert parser["strategy"]["matrix"] == "${{ fromJSON(needs.prepare.outputs.matrix) }}"
    parse_script = parser["steps"][-1]["run"]
    assert "python3 -m tools.profiling.workflow parse" in parse_script
    assert "--node-index ${{ matrix.node_index }}" in parse_script
    assert "--max-process-number 16" in parse_script

    finalize_script = finalize["steps"][-1]["run"]
    assert "python3 -m tools.profiling.workflow finalize" in finalize_script


def test_pr_nightly_dispatch_forwards_profiling_to_all_socs():
    workflow = load_workflow("pr_nightly_command.yml")
    jobs = workflow["jobs"]
    assert jobs["authorize"]["outputs"]["profile_options_json"] == "${{ steps.resolve.outputs.profile_options_json }}"
    for soc in ("a2", "a3", "a3-560t", "310p", "a5"):
        job = jobs[f"dispatch-{soc}"]
        script = next(step["run"] for step in job["steps"] if step["name"].startswith("Dispatch nightly-"))
        assert '-f profile_options_json="$PROFILE_OPTIONS_JSON"' in script
