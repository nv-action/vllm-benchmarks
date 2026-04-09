import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile

SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "ci_log_summary.py"
BISECT_SCRIPT_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "bisect_vllm.sh"
BISECT_HELPER_PATH = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "scripts" / "bisect_helper.py"


def load_module():
    spec = importlib.util.spec_from_file_location("ci_log_summary", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def load_bisect_helper(path: Path = BISECT_HELPER_PATH):
    spec = importlib.util.spec_from_file_location("bisect_helper", path)
    module = importlib.util.module_from_spec(spec)
    assert spec is not None
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_bisect_result_success_when_all_groups_agree():
    module = load_bisect_helper()

    result = module.build_bisect_result_json(
        caller_type="main2main",
        caller_run_id="24000000000",
        bisect_run_id="24000000002",
        good_commit="8a34c5087aa723eafd9995a3af814fcae8334c4d",
        bad_commit="fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
        test_cmd="pytest -sv tests/e2e/singlecard/test_sampler.py::test_x",
        group_results=[
            {
                "group": "e2e-singlecard",
                "status": "success",
                "first_bad_commit": "abc123",
                "first_bad_commit_url": "https://github.com/vllm-project/vllm/commit/abc123",
            },
            {
                "group": "e2e-4cards",
                "status": "success",
                "first_bad_commit": "abc123",
                "first_bad_commit_url": "https://github.com/vllm-project/vllm/commit/abc123",
            },
        ],
        skipped_commits=["deadbeef"],
        log_entries=[{"commit": "abc123", "result": "success"}],
    )

    assert result["caller_type"] == "main2main"
    assert result["caller_run_id"] == "24000000000"
    assert result["bisect_run_id"] == "24000000002"
    assert result["status"] == "success"
    assert result["first_bad_commit"] == "abc123"
    assert result["first_bad_commit_url"] == "https://github.com/vllm-project/vllm/commit/abc123"
    assert result["skipped_commits"] == ["deadbeef"]
    assert result["group_results"][0]["group"] == "e2e-singlecard"


def test_bisect_result_ambiguous_when_groups_disagree():
    module = load_bisect_helper()

    result = module.build_bisect_result_json(
        caller_type="main2main",
        caller_run_id="24000000000",
        bisect_run_id="24000000002",
        good_commit="good",
        bad_commit="bad",
        test_cmd="pytest -sv tests/e2e/singlecard/test_sampler.py::test_x",
        group_results=[
            {
                "group": "e2e-singlecard",
                "status": "success",
                "first_bad_commit": "abc123",
            },
            {
                "group": "e2e-4cards",
                "status": "success",
                "first_bad_commit": "def456",
            },
        ],
    )

    assert result["status"] == "ambiguous"
    assert result["first_bad_commit"] == ""
    assert [group["first_bad_commit"] for group in result["group_results"]] == ["abc123", "def456"]


def test_bisect_result_partial_success_when_some_groups_fail():
    module = load_bisect_helper()

    result = module.build_bisect_result_json(
        caller_type="main2main",
        caller_run_id="24000000000",
        bisect_run_id="24000000002",
        good_commit="good",
        bad_commit="bad",
        test_cmd="pytest -sv tests/e2e/singlecard/test_sampler.py::test_x",
        group_results=[
            {
                "group": "e2e-singlecard",
                "status": "success",
                "first_bad_commit": "abc123",
            },
            {
                "group": "e2e-4cards",
                "status": "failed",
                "first_bad_commit": "",
            },
        ],
    )

    assert result["status"] == "partial_success"
    assert result["first_bad_commit"] == "abc123"
    assert [group["status"] for group in result["group_results"]] == ["success", "failed"]


def test_bisect_result_failed_when_no_group_yields_culprit():
    module = load_bisect_helper()

    result = module.build_bisect_result_json(
        caller_type="main2main",
        caller_run_id="24000000000",
        bisect_run_id="24000000002",
        good_commit="good",
        bad_commit="bad",
        test_cmd="pytest -sv tests/e2e/singlecard/test_sampler.py::test_x",
        group_results=[
            {
                "group": "e2e-singlecard",
                "status": "failed",
                "first_bad_commit": "",
            },
            {
                "group": "e2e-4cards",
                "status": "failed",
                "first_bad_commit": "",
            },
        ],
    )

    assert result["status"] == "failed"
    assert result["first_bad_commit"] == ""
    assert result["first_bad_commit_url"] == ""


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


def test_build_bisect_payload_selects_representative_cases_per_error():
    module = load_module()

    result = {
        "run_id": 456,
        "run_url": "https://example/runs/456",
        "good_commit": "good",
        "bad_commit": "bad",
        "distinct_errors": [
            {
                "error_type": "ValueError",
                "error_message": "first",
                "failed_test_cases": [
                    "tests/e2e/a/test_alpha.py::test_case_a",
                    "tests/e2e/a/test_alpha.py::test_case_b",
                ],
            },
            {
                "error_type": "RuntimeError",
                "error_message": "second",
                "failed_test_cases": [
                    "tests/e2e/a/test_alpha.py::test_case_a",
                    "tests/e2e/b/test_beta.py::test_case_c",
                ],
            },
            {
                "error_type": "TypeError",
                "error_message": "third",
                "failed_test_cases": [
                    "tests/e2e/a/test_alpha.py::test_case_a",
                ],
            },
        ],
    }

    payload = module.build_bisect_payload(result)

    assert payload["caller_run_id"] == "456"
    assert payload["good_commit"] == "good"
    assert payload["bad_commit"] == "bad"
    assert payload["representative_test_cases"] == [
        "tests/e2e/a/test_alpha.py::test_case_a",
        "tests/e2e/b/test_beta.py::test_case_c",
        "tests/e2e/a/test_alpha.py::test_case_a",
    ]
    assert payload["test_cmds"] == [
        "pytest -sv tests/e2e/a/test_alpha.py::test_case_a",
        "pytest -sv tests/e2e/b/test_beta.py::test_case_c",
        "pytest -sv tests/e2e/a/test_alpha.py::test_case_a",
    ]
    assert (
        payload["test_cmd"]
        == "pytest -sv tests/e2e/a/test_alpha.py::test_case_a; "
        "pytest -sv tests/e2e/b/test_beta.py::test_case_c; "
        "pytest -sv tests/e2e/a/test_alpha.py::test_case_a"
    )


def test_build_bisect_payload_strips_parametrized_case_suffixes():
    module = load_module()

    result = {
        "run_id": 456,
        "run_url": "https://example/runs/456",
        "good_commit": "good",
        "bad_commit": "bad",
        "distinct_errors": [
            {
                "error_type": "ValueError",
                "error_message": "first",
                "failed_test_cases": [
                    "tests/e2e/singlecard/test_sampler.py::test_qwen3_topk[test]",
                    "tests/e2e/singlecard/test_sampler.py::test_qwen3_topk[other]",
                ],
            },
            {
                "error_type": "RuntimeError",
                "error_message": "second",
                "failed_test_cases": [
                    "tests/e2e/singlecard/test_sampler.py::test_qwen3_topk[other]",
                ],
            },
        ],
    }

    payload = module.build_bisect_payload(result)

    assert payload["representative_test_cases"] == [
        "tests/e2e/singlecard/test_sampler.py::test_qwen3_topk",
        "tests/e2e/singlecard/test_sampler.py::test_qwen3_topk",
    ]
    assert payload["test_cmds"] == [
        "pytest -sv tests/e2e/singlecard/test_sampler.py::test_qwen3_topk",
        "pytest -sv tests/e2e/singlecard/test_sampler.py::test_qwen3_topk",
    ]


def test_main_supports_bisect_json_format(monkeypatch, capsys):
    module = load_module()
    calls = []

    def fake_process_run(run_id: int, repo: str | None = None):
        calls.append((run_id, repo))
        return {
            "run_id": run_id,
            "run_url": "https://example/runs/456",
            "good_commit": "good",
            "bad_commit": "bad",
            "distinct_errors": [
                {
                    "error_type": "ValueError",
                    "error_message": "broken",
                    "failed_test_cases": ["tests/e2e/a/test_alpha.py::test_case_a"],
                }
            ],
            "failed_test_files": ["tests/e2e/a/test_alpha.py"],
            "failed_test_cases": ["tests/e2e/a/test_alpha.py::test_case_a"],
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
            "bisect-json",
        ],
    )

    module.main()

    out = capsys.readouterr()
    assert calls == [(456, "nv-action/vllm-benchmarks")]
    assert '"caller_run_id": "456"' in out.out
    assert '"test_cmd": "pytest -sv tests/e2e/a/test_alpha.py::test_case_a"' in out.out


def test_get_good_commit_reads_repo_main_workflow_via_api(monkeypatch):
    module = load_module()
    calls = []

    def fake_gh_api_json(endpoint: str, **params):
        calls.append((endpoint, params))
        if endpoint == "/repos/vllm-project/vllm-ascend/contents/.github/workflows/pr_test_full.yaml":
            assert params == {"ref": "main"}
            return {"content": "dmxsbV92ZXJzaW9uOiBbYmJiYmJiYiwgdjAuMS4wXQo="}
        raise AssertionError(f"unexpected gh api call: {endpoint}, {params}")

    monkeypatch.setattr(module, "gh_api_json", fake_gh_api_json)

    result = module.get_good_commit()

    assert result == "bbbbbbb"
    assert calls == [
        (
            "/repos/vllm-project/vllm-ascend/contents/.github/workflows/pr_test_full.yaml",
            {"ref": "main"},
        )
    ]


def test_get_good_commit_returns_none_when_api_unavailable(monkeypatch):
    module = load_module()
    calls = []

    def fake_gh_api_json(endpoint: str, **params):
        calls.append((endpoint, params))
        raise SystemExit(1)

    monkeypatch.setattr(module, "gh_api_json", fake_gh_api_json)

    result = module.get_good_commit()

    assert result is None
    assert calls == [
        (
            "/repos/vllm-project/vllm-ascend/contents/.github/workflows/pr_test_full.yaml",
            {"ref": "main"},
        )
    ]


def test_extract_bad_commit_returns_input_vllm_sha_first(monkeypatch):
    module = load_module()
    log_text = """
2026-03-16T08:28:42Z ##[group] Inputs
2026-03-16T08:28:42Z   vllm: 57431d8231235cdae89e71b4024f611858c47372
2026-03-16T08:28:42Z ##[endgroup]
2026-03-16T08:28:42Z Uses: vllm-project/vllm-ascend/.github/workflows/_e2e_test.yaml@refs/pull/7202/merge (ce306612750b2275b67df89290a8d5115859d20d)
2026-03-16T08:29:53Z [command]/usr/bin/git log -1 --format=%H
2026-03-16T08:29:53Z 65b2f405dca824adad17a42a71c908c6ebbcfd9a
""".strip()

    monkeypatch.setattr(module, "gh_api_json", lambda endpoint, **params: (_ for _ in ()).throw(AssertionError(endpoint)))

    assert module.extract_bad_commit(log_text) == "57431d8231235cdae89e71b4024f611858c47372"


def test_extract_bad_commit_uses_checkout_hash_for_branch_input(monkeypatch):
    module = load_module()
    log_text = """
2026-03-10T20:29:05Z ##[group] Inputs
2026-03-10T20:29:05Z   vllm: main
2026-03-10T20:29:05Z ##[endgroup]
2026-03-10T20:29:46Z ##[group]Run actions/checkout@v6
2026-03-10T20:29:46Z   repository: vllm-project/vllm
2026-03-10T20:29:53Z [command]/usr/bin/git log -1 --format=%H
2026-03-10T20:29:53Z 65b2f405dca824adad17a42a71c908c6ebbcfd9a
2026-03-10T20:29:54Z Uses: vllm-project/vllm-ascend/.github/workflows/_e2e_test.yaml@refs/heads/main (e16009b2cce40833b25cdd0284e85bb1984585bc)
""".strip()

    monkeypatch.setattr(module, "gh_api_json", lambda endpoint, **params: (_ for _ in ()).throw(AssertionError(endpoint)))

    assert module.extract_bad_commit(log_text) == "65b2f405dca824adad17a42a71c908c6ebbcfd9a"


def test_extract_bad_commit_ignores_vllm_ascend_checkout_hash(monkeypatch):
    module = load_module()
    log_text = """
2026-03-10T20:29:05Z ##[group] Inputs
2026-03-10T20:29:05Z   vllm: main
2026-03-10T20:29:05Z ##[endgroup]
2026-03-10T20:29:16Z ##[group]Run actions/checkout@v6
2026-03-10T20:29:16Z   repository: vllm-project/vllm-ascend
2026-03-10T20:29:18Z [command]/usr/bin/git log -1 --format=%H
2026-03-10T20:29:18Z e16009b2cce40833b25cdd0284e85bb1984585bc
2026-03-10T20:29:46Z ##[group]Run actions/checkout@v6
2026-03-10T20:29:46Z   repository: vllm-project/vllm
2026-03-10T20:29:53Z [command]/usr/bin/git log -1 --format=%H
2026-03-10T20:29:53Z 65b2f405dca824adad17a42a71c908c6ebbcfd9a
""".strip()

    monkeypatch.setattr(module, "gh_api_json", lambda endpoint, **params: (_ for _ in ()).throw(AssertionError(endpoint)))

    assert module.extract_bad_commit(log_text) == "65b2f405dca824adad17a42a71c908c6ebbcfd9a"


def test_extract_bad_commit_uses_real_checkout_hash_for_tag_input(monkeypatch):
    module = load_module()
    log_text = """
2026-03-16T08:28:55Z Uses: vllm-project/vllm-ascend/.github/workflows/_e2e_test.yaml@refs/pull/7202/merge (ce306612750b2275b67df89290a8d5115859d20d)
2026-03-16T08:28:55Z ##[group] Inputs
2026-03-16T08:28:55Z   vllm: v0.17.0
2026-03-16T08:28:55Z ##[endgroup]
2026-03-16T08:29:17Z [command]/usr/bin/git log -1 --format=%H
2026-03-16T08:29:17Z ce306612750b2275b67df89290a8d5115859d20d
2026-03-16T08:29:47Z ##[group]Run actions/checkout@v6
2026-03-16T08:29:47Z   repository: vllm-project/vllm
2026-03-16T08:29:53Z [command]/usr/bin/git log -1 --format=%H
2026-03-16T08:29:53Z b31e9326a7d9394aab8c767f8ebe225c65594b60
""".strip()

    monkeypatch.setattr(module, "gh_api_json", lambda endpoint, **params: (_ for _ in ()).throw(AssertionError(endpoint)))

    assert module.extract_bad_commit(log_text) == "b31e9326a7d9394aab8c767f8ebe225c65594b60"


def test_extract_bad_commit_falls_back_to_workflow_ref_when_needed(monkeypatch):
    module = load_module()
    log_text = """
2026-03-16T08:28:42Z Uses: vllm-project/vllm-ascend/.github/workflows/_e2e_test.yaml@refs/pull/7202/merge (ce306612750b2275b67df89290a8d5115859d20d)
2026-03-16T08:28:42Z ##[group] Inputs
2026-03-16T08:28:42Z   vllm: v0.18.0
2026-03-16T08:28:42Z ##[endgroup]
""".strip()
    calls = []

    def fake_gh_api_json(endpoint: str, **params):
        calls.append((endpoint, params))
        if endpoint == "/repos/vllm-project/vllm-ascend/contents/.github/workflows/pr_test_full.yaml":
            assert params == {"ref": "ce306612750b2275b67df89290a8d5115859d20d"}
            return {
                "content": "dmxsbV92ZXJzaW9uOiBbNTc0MzFkODIzMTIzNWNkYWU4OWU3MWI0MDI0ZjYxMTg1OGM0NzM3MiwgdjAuMTguMF0K"
            }
        raise AssertionError(f"unexpected gh api call: {endpoint}, {params}")

    monkeypatch.setattr(module, "gh_api_json", fake_gh_api_json)

    assert module.extract_bad_commit(log_text) == "57431d8231235cdae89e71b4024f611858c47372"
    assert calls == [
        (
            "/repos/vllm-project/vllm-ascend/contents/.github/workflows/pr_test_full.yaml",
            {"ref": "ce306612750b2275b67df89290a8d5115859d20d"},
        )
    ]


def test_process_local_log_extracts_run_suite_file_failures(monkeypatch):
    module = load_module()
    log_text = """
[1/10] FAILED (exit code 4)  tests/e2e/multicard/4-cards/test_kimi_k2.py  (26s)
Summary: 0/10 passed  (26.38s total)
""".strip()

    monkeypatch.setattr(module, "get_good_commit", lambda: "good")
    monkeypatch.setattr(module, "extract_bad_commit", lambda log_text, resolve_remote=False: "bad")

    result = module.process_local_log(log_text)

    assert result["failed_test_cases"] == []
    assert result["failed_test_files"] == ["tests/e2e/multicard/4-cards/test_kimi_k2.py"]


def test_process_local_log_extracts_run_suite_case_failures(monkeypatch):
    module = load_module()
    log_text = """
[1/27] FAILED (exit code 4)  tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2  (26s)
Summary: 0/27 passed  (26.39s total)
""".strip()

    monkeypatch.setattr(module, "get_good_commit", lambda: "good")
    monkeypatch.setattr(module, "extract_bad_commit", lambda log_text, resolve_remote=False: "bad")

    result = module.process_local_log(log_text)

    assert result["failed_test_cases"] == [
        "tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2"
    ]
    assert result["failed_test_files"] == ["tests/e2e/multicard/2-cards/test_offline_inference_distributed.py"]


def test_process_local_log_extracts_conftest_import_error_for_case(monkeypatch):
    module = load_module()
    log_text = """
[1/27] START  tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2
ImportError while loading conftest '/__w/vllm-ascend/vllm-ascend/tests/e2e/conftest.py'.
tests/e2e/conftest.py:50: in <module>
    from vllm import LLM, SamplingParams
vllm-empty/vllm/env_override.py:507: in <module>
    from torch._dynamo.convert_frame import GraphCaptureOutput
E   ImportError: cannot import name 'GraphCaptureOutput' from 'torch._dynamo.convert_frame'
[1/27] FAILED (exit code 4)  tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2  (26s)
""".strip()

    monkeypatch.setattr(module, "get_good_commit", lambda: "good")
    monkeypatch.setattr(module, "extract_bad_commit", lambda log_text, resolve_remote=False: "bad")

    result = module.process_local_log(log_text)

    assert result["failed_test_cases"] == [
        "tests/e2e/multicard/2-cards/test_offline_inference_distributed.py::test_qwen3_dense_fc1_tp2"
    ]
    assert len(result["code_bugs"]) == 1
    assert result["code_bugs"][0]["error_type"] == "ImportError"
    assert "GraphCaptureOutput" in result["code_bugs"][0]["error_message"]


def test_process_local_log_extracts_file_scope_error_without_case(monkeypatch):
    module = load_module()
    log_text = """
[1/10] START  tests/e2e/multicard/4-cards/test_kimi_k2.py
RuntimeError: NPU out of memory
[1/10] FAILED (exit code 4)  tests/e2e/multicard/4-cards/test_kimi_k2.py  (26s)
""".strip()

    monkeypatch.setattr(module, "get_good_commit", lambda: "good")
    monkeypatch.setattr(module, "extract_bad_commit", lambda log_text, resolve_remote=False: "bad")

    result = module.process_local_log(log_text)

    assert result["failed_test_cases"] == []
    assert result["failed_test_files"] == ["tests/e2e/multicard/4-cards/test_kimi_k2.py"]
    assert len(result["code_bugs"]) == 1
    assert result["code_bugs"][0]["error_type"] == "RuntimeError"
    assert result["code_bugs"][0]["failed_test_files"] == ["tests/e2e/multicard/4-cards/test_kimi_k2.py"]
    assert result["code_bugs"][0]["failed_test_cases"] == []


def test_gh_api_raw_retries_eof_once(monkeypatch, capsys):
    module = load_module()
    calls = []

    def fake_run(args, capture_output=True, text=True, check=True):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr='Get "https://api.github.com/x": EOF')
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="payload", stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.gh_api_raw("/repos/example/logs")

    assert result == "payload"
    assert len(calls) == 2
    assert capsys.readouterr().err == ""


def test_gh_api_json_retries_eof_once(monkeypatch, capsys):
    module = load_module()
    calls = []

    def fake_run(args, capture_output=True, text=True, check=True):
        calls.append(args)
        if len(calls) == 1:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, stderr='Get "https://api.github.com/x": EOF')
        return subprocess.CompletedProcess(args=args, returncode=0, stdout='{"ok": true}', stderr="")

    monkeypatch.setattr(module.subprocess, "run", fake_run)

    result = module.gh_api_json("/repos/example/run")

    assert result == {"ok": True}
    assert len(calls) == 2
    assert capsys.readouterr().err == ""


def test_bisect_script_does_not_exit_during_parse_args_for_test_cmds_file(tmp_path):
    cmds_file = tmp_path / "cmds.txt"
    cmds_file.write_text("pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization::test_initialization_eagle_graph\n")

    result = subprocess.run(
        [
            "bash",
            str(BISECT_SCRIPT_PATH),
            "--test-cmds-file",
            str(cmds_file),
            "--vllm-repo",
            "/nonexistent/vllm",
            "--ascend-repo",
            "/nonexistent/ascend",
            "--no-fetch",
            "--good",
            "8a34c5087aa723eafd9995a3af814fcae8334c4d",
            "--bad",
            "c6f722b93e8e795065751172812ee6a5540e5901",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Batch bisect: 1 test command(s)" in result.stdout


def test_bisect_script_does_not_exit_during_detect_commits_when_commits_are_provided():
    with tempfile.TemporaryDirectory() as tmp_dir:
        vllm_repo = Path(tmp_dir) / "vllm"
        ascend_repo = Path(tmp_dir) / "ascend"
        vllm_repo.mkdir()
        ascend_repo.mkdir()
        subprocess.run(["git", "init", str(vllm_repo)], check=True, capture_output=True, text=True)

        result = subprocess.run(
            [
                "bash",
                str(BISECT_SCRIPT_PATH),
                "--test-cmd",
                "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization::test_initialization_eagle_graph",
                "--vllm-repo",
                str(vllm_repo),
                "--ascend-repo",
                str(ascend_repo),
                "--no-fetch",
                "--good",
                "8a34c5087aa723eafd9995a3af814fcae8334c4d",
                "--bad",
                "c6f722b93e8e795065751172812ee6a5540e5901",
            ],
            capture_output=True,
            text=True,
        )

    assert result.returncode != 0
    assert "Preparing vllm repo for bisect" in result.stdout
    assert "Auto-detecting good commit" not in result.stdout
    assert "Auto-detecting bad commit" not in result.stdout


def test_bisect_helper_finds_repo_root_from_cwd_when_copied(tmp_path):
    helper_copy = tmp_path / "bisect_helper.py"
    helper_copy.write_text(BISECT_HELPER_PATH.read_text())
    module = load_bisect_helper(helper_copy)

    assert module._get_repo_root(cwd=BISECT_HELPER_PATH.parents[3]) == BISECT_HELPER_PATH.parents[3]


def test_bisect_script_prefers_editable_project_location_for_ascend_repo(tmp_path):
    editable_repo = tmp_path / "vllm-ascend-editable"
    location_repo = tmp_path / "site-packages"
    vllm_repo = tmp_path / "vllm"
    editable_repo.mkdir()
    location_repo.mkdir()
    vllm_repo.mkdir()
    subprocess.run(["git", "init", str(vllm_repo)], check=True, capture_output=True, text=True)

    fake_pip = tmp_path / "pip"
    fake_pip.write_text(
        "\n".join(
            [
                "#!/bin/sh",
                'if [ \"$1\" = \"show\" ] && [ \"$2\" = \"vllm-ascend\" ]; then',
                "  cat <<'EOF'",
                "Name: vllm_ascend",
                f"Location: {location_repo}",
                f"Editable project location: {editable_repo}",
                "EOF",
                "  exit 0",
                "fi",
                "exec /usr/bin/env pip \"$@\"",
            ]
        )
        + "\n"
    )
    fake_pip.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{tmp_path}:{env['PATH']}"

    result = subprocess.run(
        [
            "bash",
            str(BISECT_SCRIPT_PATH),
            "--test-cmd",
            "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization::test_initialization_eagle_graph",
            "--vllm-repo",
            str(vllm_repo),
            "--no-fetch",
            "--good",
            "8a34c5087aa723eafd9995a3af814fcae8334c4d",
            "--bad",
            "c6f722b93e8e795065751172812ee6a5540e5901",
        ],
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode != 0
    assert f"Auto-detected vllm-ascend repo via pip show: {editable_repo}" in result.stdout


def test_bisect_script_exits_cleanly_when_commit_cannot_be_resolved():
    result = subprocess.run(
        [
            "bash",
            str(BISECT_SCRIPT_PATH),
            "--test-cmd",
            "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization::test_initialization_eagle_graph",
            "--vllm-repo",
            "/Users/antarctica/Work/PR/vllm",
            "--ascend-repo",
            "/Users/antarctica/Work/PR/vllm-benchmarks",
            "--no-fetch",
            "--good",
            "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
            "--bad",
            "c6f722b93e8e795065751172812ee6a5540e5901",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "Cannot resolve good commit" in result.stderr
    assert "Good commit resolved" not in result.stdout


def test_bisect_script_refuses_dirty_vllm_repo(tmp_path):
    dirty_repo = tmp_path / "vllm"
    dirty_repo.mkdir()
    subprocess.run(["git", "init"], cwd=dirty_repo, check=True, capture_output=True, text=True)
    (dirty_repo / "README.md").write_text("dirty\n")

    result = subprocess.run(
        [
            "bash",
            str(BISECT_SCRIPT_PATH),
            "--test-cmd",
            "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization::test_initialization_eagle_graph",
            "--vllm-repo",
            str(dirty_repo),
            "--ascend-repo",
            "/Users/antarctica/Work/PR/vllm-benchmarks",
            "--no-fetch",
            "--good",
            "8a34c5087aa723eafd9995a3af814fcae8334c4d",
            "--bad",
            "c6f722b93e8e795065751172812ee6a5540e5901",
        ],
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    assert "vllm repo has uncommitted or unmerged changes" in result.stderr


def test_bisect_helper_returns_workflow_vllm_install_for_test_cmd():
    module = load_bisect_helper()

    install_cmd = module.get_vllm_install_for_test_cmd(
        "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization"
    )

    assert "uv pip install" in install_cmd
    assert "uv pip uninstall triton" in install_cmd
    assert "VLLM_TARGET_DEVICE=empty" in install_cmd


def test_bisect_helper_returns_workflow_ascend_install_for_test_cmd():
    module = load_bisect_helper()

    install_cmd = module.get_ascend_install_for_test_cmd(
        "pytest -sv tests/ut/spec_decode/test_eagle_proposer.py::TestEagleProposerInitialization"
    )

    assert "uv pip install -v ." in install_cmd
    assert "uv pip install -r requirements-dev.txt" in install_cmd
