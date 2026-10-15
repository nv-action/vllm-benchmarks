"""Private PR validation must use the requested code and immutable images."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[4]
WORKFLOWS = ROOT / ".github/workflows"
PR_REF = "${{ inputs.vllm_ascend_ref || github.sha }}"


def workflow(name):
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8-sig"), Loader=yaml.BaseLoader)


def test_only_explicit_triggers_remain():
    for path in WORKFLOWS.glob("*.y*ml"):
        events = workflow(path.name)["on"]
        assert not {"schedule", "push", "pull_request", "pull_request_target", "workflow_run"}.intersection(events)
        if "issue_comment" in events:
            assert path.name == "slash_command_dispatch.yml"
    assert "repository_dispatch" not in workflow("schedule_daily_test.yaml")["on"]


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


def test_validation_benchmarks_preserve_load_and_warmup():
    configs = ROOT / "tests/e2e/cases/models/configs"
    deepseek = yaml.safe_load((configs / "DeepSeek/DeepSeek-V4.1-Flash-w8a8-PD-prefix.yaml").read_text())
    assert deepseek["dp_load_balancing"] == "external"
    assert deepseek["num_nodes"] == 2
    benchmarks = deepseek["benchmarks"]
    assert list(benchmarks) == ["perf_TPOT50_128k_1_prefix90_warmup", "perf_TPOT50_16k_1"]
    assert benchmarks["perf_TPOT50_128k_1_prefix90_warmup"]["num_prompts"] == 4
    perf = benchmarks["perf_TPOT50_16k_1"]
    assert (perf["num_prompts"], perf["batch_size"], perf["request_rate"]) == (192, 64, 2.7)
    assert "test_content" not in deepseek
    glm = yaml.safe_load((configs / "GLM/GLM-5.2-W8A8C8-A3_3.5k_1.5k_0_20.yaml").read_text())
    assert glm["dp_load_balancing"] == "internal"
    assert glm["num_nodes"] == 2
    assert list(glm["benchmarks"]) == ["perf_TPOT20"]
    for soc in ("A2", "A3"):
        qwen = yaml.safe_load((configs / f"Qwen/Qwen3.8-27B-w8a8-{soc}.yaml").read_text())
        assert list(qwen["test_cases"][0]["benchmarks"]) == ["perf"]
