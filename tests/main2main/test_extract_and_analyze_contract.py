import importlib.util
from pathlib import Path


SCRIPT_PATH = (
    Path(__file__).resolve().parents[2]
    / ".github"
    / "workflows"
    / "scripts"
    / "ci_log_summary.py"
)


def load_module():
    spec = importlib.util.spec_from_file_location("ci_log_summary", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_process_run_accepts_repo_override(monkeypatch):
    module = load_module()
    captured = []

    def fake_gh_api_json(endpoint: str, **params):
        captured.append((endpoint, params))
        if endpoint == "/repos/nv-action/vllm-benchmarks/actions/runs/123/jobs":
            return {"jobs": []}
        if endpoint == "/repos/nv-action/vllm-benchmarks/actions/runs/123":
            return {"html_url": "https://example/runs/123", "created_at": "2026-03-12T00:00:00Z"}
        raise AssertionError(f"unexpected endpoint: {endpoint}")

    monkeypatch.setattr(module, "gh_api_json", fake_gh_api_json)
    monkeypatch.setattr(module, "get_good_commit", lambda: "good")

    result = module.process_run(123, repo="nv-action/vllm-benchmarks")

    assert result["run_id"] == 123
    assert captured[0][0] == "/repos/nv-action/vllm-benchmarks/actions/runs/123"
    assert captured[1][0] == "/repos/nv-action/vllm-benchmarks/actions/runs/123/jobs"


def test_main_uses_repo_flag_with_explicit_run_id(monkeypatch, capsys):
    module = load_module()
    calls = []

    def fake_process_run(run_id: int, repo: str | None = None):
        calls.append((run_id, repo))
        return {
            "run_id": run_id,
            "run_url": "https://example/runs/456",
            "good_commit": "good",
            "bad_commit": "bad",
            "failed_test_files": [],
            "failed_test_cases": [],
            "code_bugs": [],
            "env_flakes": [],
        }

    monkeypatch.setattr(module, "process_run", fake_process_run)
    monkeypatch.setattr(
        module.sys,
        "argv",
        [
            "ci_log_summary.py",
            "--repo",
            "nv-action/vllm-benchmarks",
            "--run-id",
            "456",
            "--format",
            "json",
        ],
    )

    module.main()

    out = capsys.readouterr()
    assert calls == [(456, "nv-action/vllm-benchmarks")]
    assert '"run_id": 456' in out.out
