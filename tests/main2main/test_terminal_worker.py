import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from state_store import JsonStore
from terminal_worker import TerminalJob, TerminalWorker


def _make_worker(store, **overrides):
    defaults = {
        "extract_fn": lambda **kw: {"summary": "test"},
        "summarize_fn": lambda **kw: "test body",
        "create_issue_fn": lambda **kw: "https://github.com/issue/1",
        "find_existing_issue_fn": lambda **kw: None,
        "update_state_fn": lambda pr_number, status: None,
        "build_phase3_issue_fn": lambda **kw: "phase3 body",
        "service_lock": None,
    }
    defaults.update(overrides)
    return TerminalWorker(store=store, **defaults)


def test_enqueue_persists_job_to_store():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        worker = _make_worker(store)
        job = TerminalJob(
            pr_number=154,
            repo="nv-action/vllm-benchmarks",
            terminal_reason="done_failure",
            e2e_run_id="12345",
            e2e_run_url="https://example.com",
            fixup_run_id=None,
        )
        worker.enqueue(job)
        data = store.load()
        assert len(data.get("_terminal_jobs", [])) == 1
        assert data["_terminal_jobs"][0]["pr_number"] == 154
        assert data["_terminal_jobs"][0]["status"] == "pending"


def test_pending_count_reflects_queue():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        worker = _make_worker(store)
        assert worker.pending_count() == 0
        worker.enqueue(
            TerminalJob(
                pr_number=1,
                repo="r",
                terminal_reason="done_failure",
                e2e_run_id="1",
                e2e_run_url="u",
                fixup_run_id=None,
            )
        )
        assert worker.pending_count() == 1


def test_process_done_failure_job_calls_extract_and_summarize():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        calls = []

        def mock_extract(**kw):
            calls.append(("extract", kw))
            return {"summary": "x"}

        def mock_summarize(**kw):
            calls.append(("summarize", kw))
            return "issue body"

        def mock_create(**kw):
            calls.append(("create", kw))
            return "https://github.com/issue/1"

        worker = _make_worker(
            store,
            extract_fn=mock_extract,
            summarize_fn=mock_summarize,
            create_issue_fn=mock_create,
        )
        worker.enqueue(
            TerminalJob(
                pr_number=154,
                repo="r",
                terminal_reason="done_failure",
                e2e_run_id="999",
                e2e_run_url="u",
                fixup_run_id=None,
            )
        )
        asyncio.run(worker.process_one())
        assert any(call[0] == "extract" for call in calls)
        assert any(call[0] == "summarize" for call in calls)
        assert any(call[0] == "create" for call in calls)
        assert worker.pending_count() == 0


def test_process_phase3_no_changes_skips_extract():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        calls = []

        def mock_extract(**kw):
            calls.append("extract")
            return {}

        def mock_create(**kw):
            return "https://github.com/issue/1"

        worker = _make_worker(
            store,
            extract_fn=mock_extract,
            create_issue_fn=mock_create,
        )
        worker.enqueue(
            TerminalJob(
                pr_number=154,
                repo="r",
                terminal_reason="phase3_no_changes",
                e2e_run_id=None,
                e2e_run_url=None,
                fixup_run_id="run1",
            )
        )
        asyncio.run(worker.process_one())
        assert "extract" not in calls
        assert worker.pending_count() == 0


def test_idempotency_skips_creation_when_issue_exists():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        create_calls = []

        def mock_find(**kw):
            return "https://github.com/existing/1"

        def mock_create(**kw):
            create_calls.append(1)
            return "https://github.com/issue/new"

        worker = _make_worker(
            store,
            find_existing_issue_fn=mock_find,
            create_issue_fn=mock_create,
        )
        worker.enqueue(
            TerminalJob(
                pr_number=154,
                repo="r",
                terminal_reason="done_failure",
                e2e_run_id="999",
                e2e_run_url="u",
                fixup_run_id=None,
            )
        )
        asyncio.run(worker.process_one())
        assert len(create_calls) == 0
        assert worker.pending_count() == 0


def test_process_one_updates_state_under_service_lock():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        lock = asyncio.Lock()
        states = []

        worker = _make_worker(
            store,
            service_lock=lock,
            update_state_fn=lambda pr_number, status: states.append((pr_number, status)),
        )
        worker.enqueue(
            TerminalJob(
                pr_number=154,
                repo="r",
                terminal_reason="done_failure",
                e2e_run_id="999",
                e2e_run_url="u",
                fixup_run_id=None,
            )
        )
        asyncio.run(worker.process_one())
        assert states == [(154, "manual_review")]


def test_reload_pending_jobs_on_init():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save(
            {
                "_terminal_jobs": [
                    {
                        "pr_number": 154,
                        "repo": "r",
                        "terminal_reason": "done_failure",
                        "e2e_run_id": "999",
                        "e2e_run_url": "u",
                        "fixup_run_id": None,
                        "status": "pending",
                    }
                ]
            }
        )
        worker = _make_worker(store)
        assert worker.pending_count() == 1
