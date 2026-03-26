import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from mcp_server import build_mcp_server


class FakeStore:
    def __init__(self):
        self.path = Path("/tmp/fake-state.json")
        self._data = {}

    def get(self, repo, pr_number):
        return self._data.get(f"{repo}#{pr_number}")

    def load_all(self):
        return self._data


class FakeGitHub:
    def __init__(self):
        self._prs = []

    def list_open_pr_numbers(self, repo, label="main2main"):
        return self._prs


class FakeService:
    def __init__(self):
        self.run_once_calls = []
        self.reconcile_calls = []
        self.register_calls = []

    def run_once(self, repo):
        self.run_once_calls.append(repo)
        return {"reconciled": {}, "registered": [], "fixup_outcomes": {}}

    def reconcile(self, repo, pr_number):
        self.reconcile_calls.append((repo, pr_number))
        return {"action": "wait", "reason": "test"}

    def register_from_pr_comment(self, repo, pr_number):
        self.register_calls.append((repo, pr_number))
        return {"action": "register", "phase": "2", "reason": "test"}


def test_build_server_exposes_all_six_tools():
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    tool_names = {tool["name"] for tool in server.get_tools()}

    assert tool_names == {
        "orchestrator_list_prs",
        "orchestrator_get_pr_state",
        "orchestrator_get_health",
        "orchestrator_run_once",
        "orchestrator_reconcile_pr",
        "orchestrator_register_pr",
    }


def test_get_health_returns_expected_schema():
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    result = asyncio.run(server.handle_tool_call("orchestrator_get_health", {}))
    health = json.loads(result)

    assert "state_file_exists" in health
    assert "tracked_pr_count" in health
    assert "terminal_jobs_pending" in health
    assert "uptime_seconds" in health


def test_get_pr_state_returns_none_for_unknown_pr():
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    result = asyncio.run(
        server.handle_tool_call(
            "orchestrator_get_pr_state",
            {"repo": "r", "pr_number": 999},
        )
    )

    assert json.loads(result) is None


def test_run_once_delegates_to_service():
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    asyncio.run(
        server.handle_tool_call(
            "orchestrator_run_once",
            {"repo": "nv-action/vllm-benchmarks"},
        )
    )

    assert service.run_once_calls == ["nv-action/vllm-benchmarks"]


def test_run_once_returns_error_on_exception():
    service = FakeService()
    service.run_once = lambda repo: (_ for _ in ()).throw(ValueError("test error"))
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    result = asyncio.run(
        server.handle_tool_call(
            "orchestrator_run_once",
            {"repo": "r"},
        )
    )
    parsed = json.loads(result)

    assert "error" in parsed
