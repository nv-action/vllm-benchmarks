import os
import subprocess
import sys
import tarfile
from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[3] / ".github" / "workflows"


def load_workflow(name: str) -> dict:
    return yaml.load((WORKFLOWS / name).read_text(), Loader=yaml.BaseLoader)


def test_private_a3_nightly_uses_registered_runner():
    workflow = load_workflow("schedule_nightly_test_a3.yaml")
    assert "linux-aarch64-a3-0" not in (WORKFLOWS / "schedule_nightly_test_a3.yaml").read_text()
    assert workflow["jobs"]["parse-trigger"]["runs-on"] == "linux-aarch64-a3-800t-0"
    for name in ("multi-node-tests", "double-node-tests"):
        assert workflow["jobs"][name]["with"]["runner"] == "linux-aarch64-a3-800t-0"
    reusable = load_workflow("_e2e_nightly_multi_node.yaml")
    assert reusable["on"]["workflow_call"]["inputs"]["runner"]["default"] == "linux-aarch64-a3-800t-0"


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
    assert "tools.profiling.storage status" not in script
    assert "esdk-obs-python" not in script
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
    for job in (prepare, finalize):
        assert job["runs-on"] == "linux-aarch64-cpu-4"
        assert job["container"]["image"].endswith(":nightly-cpu-py3.12")
    assert parser["runs-on"] == "linux-arm64-cpu-16"
    assert parser["container"]["image"] == "${{ inputs.image }}"

    assert prepare["if"] == "${{ inputs.should_run }}"
    assert prepare["outputs"]["matrix"] == "${{ steps.plan.outputs.matrix }}"
    plan_script = prepare["steps"][-1]["run"]
    assert "tools.profiling.storage status" not in plan_script
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
    assert "--profile-scope" not in (WORKFLOWS / "pr_nightly_command.yml").read_text()
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


def test_private_multi_node_overlay_is_scoped_to_each_run():
    job = load_workflow("_e2e_nightly_multi_node.yaml")["jobs"]["e2e"]
    steps = job["steps"]
    prepare = next(step for step in steps if step["name"] == "Prepare scripts")
    script = prepare["run"]
    assert 'script_dir="$LOG_PREFIX/validation-source"' in script
    assert "fromJSON(inputs.profile_options_json).enabled && inputs.request_id == ''" in script
    assert 'git config --global --add safe.directory "$(pwd -P)"' in script
    assert "git -c safe.directory" not in script
    assert "git archive --format=tar HEAD" in script
    for path in (
        "tools/profiling",
        "tools/aisbench.py",
        "tests/e2e/common/multi_node",
        "tests/e2e/nightly/scripts/result_postprocess.py",
    ):
        assert path in script
    assert '"$config_path" > "$script_dir/profile-overlay.tar"' in script
    assert "vllm_ascend/worker" not in script
    assert steps.index(prepare) < next(i for i, step in enumerate(steps) if step["name"] == "Launch cluster")
    root = WORKFLOWS.parents[1]
    template = (root / "tests/e2e/common/multi_node/lws.yaml.jinja2").read_text()
    assert template.count('bash "$LOG_PREFIX/validation-source/run.sh"') == 2
    assert "/root/.cache/tests/run.sh" not in template
    run_script = (root / "tests/e2e/common/multi_node/run.sh").read_text()
    assert "    overlay_profiling_source\n    show_vllm_info" in run_script


def test_overlay_archive_handles_different_repository_owner(tmp_path):
    root = WORKFLOWS.parents[1]
    steps = load_workflow("_e2e_nightly_multi_node.yaml")["jobs"]["e2e"]["steps"]
    script = next(step["run"] for step in steps if step["name"] == "Prepare scripts")
    archive_script = script[script.index("  git config --global ") : script.index('  echo "[Profiling]')]
    config = "tests/e2e/cases/models/configs/GLM/GLM-5.2-W8A8C8-A3_3.5k_1.5k_0_20.yaml"
    env = {
        **os.environ,
        "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1",
        "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"),
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GITHUB_WORKSPACE": str(root),
        "script_dir": str(tmp_path),
        "config_path": config,
    }
    unsafe = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, env=env, text=True, capture_output=True)
    assert unsafe.returncode != 0
    assert "dubious ownership" in unsafe.stderr
    result = subprocess.run(["bash", "-eu", "-c", archive_script], cwd=root, env=env, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    trusted = subprocess.run(
        ["git", "config", "--global", "--get-all", "safe.directory"],
        cwd=root,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert trusted.stdout.strip() == str(root.resolve())
    assert len((tmp_path / "profile-source.commit").read_text().strip()) == 40
    with tarfile.open(tmp_path / "profile-overlay.tar") as archive:
        assert config in archive.getnames()
        assert "tools/profiling/capture.py" in archive.getnames()
        assert "tools/aisbench.py" in archive.getnames()


@pytest.mark.parametrize(
    ("enabled", "is_pr", "node", "has_archive", "expected"),
    [
        ("true", "false", "0", True, "copied"),
        ("true", "false", "1", True, "copied"),
        ("false", "false", "0", False, "skipped"),
        ("true", "true", "0", False, "skipped"),
        ("true", "false", "0", False, "failed"),
    ],
)
def test_multi_node_overlay_copies_source_without_changing_runtime(
    tmp_path, enabled, is_pr, node, has_archive, expected
):
    root = WORKFLOWS.parents[1]
    run_script = (root / "tests/e2e/common/multi_node/run.sh").read_text()
    overlay = (
        "overlay_profiling_source() {"
        + run_script.split("overlay_profiling_source() {", 1)[1].split("\ninstall_vllm_ascend()", 1)[0]
    )
    source = tmp_path / "validation-source"
    source.mkdir()
    target = tmp_path / "workspace/vllm-ascend"
    target.mkdir(parents=True)
    files = (
        "tools/aisbench.py",
        "tools/profiling/capture.py",
        "tests/e2e/common/multi_node/internal_dp/test_multi_node.py",
        "tests/e2e/common/multi_node/external_dp/runtime.py",
        "tests/e2e/cases/models/configs/GLM/case.yaml",
    )
    for name in files:
        path = target / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# image source\n")
    runtime = target / "vllm_ascend/worker/worker.py"
    runtime.parent.mkdir(parents=True)
    runtime.write_text("# installed runtime\n")
    if has_archive:
        payload = tmp_path / "payload"
        with tarfile.open(source / "profile-overlay.tar", "w") as archive:
            for name in files:
                path = payload / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("# validation source\n")
                archive.add(path, arcname=name)
        (source / "profile-source.commit").write_text("validation-sha\n")
    result = subprocess.run(
        ["bash", "-eu", "-c", 'print_section() { echo "$1"; }\n' + overlay + "\noverlay_profiling_source"],
        env={
            **os.environ,
            "PATH": f"{Path(sys.executable).parent}:{os.environ['PATH']}",
            "SCRIPT_DIR": str(source),
            "WORKSPACE": str(target.parent),
            "IS_PR_TEST": is_pr,
            "ASCEND_PROFILE_ENABLED": enabled,
            "LWS_WORKER_INDEX": node,
            "CONFIG_BASE_PATH": "tests/e2e/cases/models/configs/GLM",
            "CONFIG_YAML_PATH": "case.yaml",
        },
        text=True,
        capture_output=True,
    )
    assert (result.returncode != 0) == (expected == "failed"), result.stderr
    for name in files:
        assert (target / name).read_text() == ("# validation source\n" if expected == "copied" else "# image source\n")
    assert runtime.read_text() == "# installed runtime\n"
    if expected == "copied":
        assert f"node {node}" in result.stdout
        assert "Validation source SHA: validation-sha" in result.stdout
        assert str(target / files[-1]) in result.stdout


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


def test_a3_profiling_runs_queue_without_cancelling_previous_run():
    workflow = load_workflow("schedule_nightly_test_a3.yaml")
    assert (
        workflow["concurrency"]["cancel-in-progress"] == "${{ !fromJSON(inputs.profile_options_json || '{}').enabled }}"
    )
    assert "inputs.test_cases" not in workflow["concurrency"]["group"]


def test_glm_validation_case_is_registered_as_two_nodes():
    config = load_workflow("configs/nightly_config.yaml")
    case = next(
        case
        for case in config["a3"]["double_node"]["test_config"]
        if case["name"] == "GLM-5.2-W8A8C8-A3_3.5k_1.5k_0_20"
    )
    assert case["config_file_path"] == "GLM-5.2-W8A8C8-A3_3.5k_1.5k_0_20.yaml"
    assert case["size"] == "2"


def test_deepseek_validation_runs_warmup_before_selected_perf():
    config_path = (
        WORKFLOWS.parents[1] / "tests/e2e/cases/models/configs/DeepSeek/DeepSeek-V4.1-Flash-w8a8-PD-prefix.yaml"
    )
    benchmarks = yaml.safe_load(config_path.read_text())["benchmarks"]
    assert list(benchmarks) == ["perf_TPOT50_128k_1_prefix90_warmup", "perf_TPOT50_16k_1"]
    warmup = benchmarks["perf_TPOT50_128k_1_prefix90_warmup"]
    assert warmup["num_prompts"] == warmup["batch_size"] == 4
    assert warmup["max_out_len"] == 1
    perf = benchmarks["perf_TPOT50_16k_1"]
    assert perf["num_prompts"] == 256
    assert perf["batch_size"] == 64
    assert perf["request_rate"] == 2.7
