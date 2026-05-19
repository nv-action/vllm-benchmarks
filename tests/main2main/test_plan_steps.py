import importlib.util
from pathlib import Path

import pytest


SCRIPT = (
    Path(__file__).resolve().parents[2]
    / ".agents"
    / "skills"
    / "main2main"
    / "scripts"
    / "plan_steps.py"
)


def load_plan_steps():
    spec = importlib.util.spec_from_file_location("main2main_plan_steps", SCRIPT)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "path",
    [
        "pyproject.toml",
        "setup.py",
        "requirements/common.txt",
        "requirements/build/cuda.txt",
    ],
)
def test_classify_conservative_dependency_files_as_requirements(path: str) -> None:
    plan_steps = load_plan_steps()

    assert plan_steps._classify_file(path) == "requirements"


@pytest.mark.parametrize(
    "path",
    [
        "requirements/cuda.txt",
        "requirements/rocm.txt",
        "requirements/xpu.txt",
        "requirements/tpu.txt",
        "requirements/cpu.txt",
        "requirements-dev.txt",
        "docs/requirements.txt",
        "examples/requirements.txt",
    ],
)
def test_classify_platform_specific_dependency_files_as_ignored(path: str) -> None:
    plan_steps = load_plan_steps()

    assert plan_steps._classify_file(path) == "ignored"


def test_commit_count_budget_scales_sublinearly_with_line_budget() -> None:
    plan_steps = load_plan_steps()

    assert plan_steps._commit_count_budget(250) == 5
    assert plan_steps._commit_count_budget(500) == 7
    assert plan_steps._commit_count_budget(1000) == 10
    assert plan_steps._commit_count_budget(2000) == 14
    assert plan_steps._commit_count_budget(4000) == 20


def test_plan_steps_limits_each_step_to_default_commit_budget() -> None:
    plan_steps = load_plan_steps()
    commits = [
        {"sha": f"{i:040x}", "subject": f"commit {i}"}
        for i in range(11)
    ]
    stats_per_commit = {
        commit["sha"]: {
            "vllm_changed_lines": 1,
            "total_changed_lines": 1,
            "categories": ["vllm"],
            "has_requirements": False,
            "files": [f"vllm/file_{i}.py"],
        }
        for i, commit in enumerate(commits)
    }

    steps = plan_steps.plan_steps(commits, stats_per_commit, "base")

    assert [step["commit_count"] for step in steps] == [10, 1]
    assert all(
        step["commit_count"] <= plan_steps._commit_count_budget(plan_steps.LINE_BUDGET)
        for step in steps
    )
    assert {step["commit_count_budget"] for step in steps} == {10}


def test_render_markdown_displays_step_commit_count() -> None:
    plan_steps = load_plan_steps()
    plan = {
        "base_commit": "base",
        "target_commit": "target",
        "total_commits": 2,
        "steps": [
            {
                "id": "step-1",
                "commit_count": 2,
                "vllm_changed_lines": 20,
                "total_changed_lines": 30,
                "categories": ["vllm"],
                "start_commit": "base",
                "end_commit": "abc123456789",
                "commits": [
                    {"sha": "a" * 40, "subject": "first"},
                    {"sha": "b" * 40, "subject": "second"},
                ],
            },
        ],
    }

    markdown = plan_steps._render_markdown(plan)

    assert "commits: 2" in markdown
