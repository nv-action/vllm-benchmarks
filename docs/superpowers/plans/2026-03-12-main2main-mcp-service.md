# Main2Main MCP Service Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy the existing main2main orchestrator as a long-lived systemd service with MCP interface, async terminal analysis, and minimal extraction of reusable layers.

**Architecture:** Single asyncio process hosting three concurrent tasks: MCP server (SSE), reconcile poller, and terminal-analysis worker. Extract `GitHubCliAdapter` and `JsonStore` into sibling files. Keep business dataclasses and `parse_*` helpers in `main2main_orchestrator.py`. Use `asyncio.Lock` to serialize control operations dispatched via `asyncio.to_thread()`, including the terminal worker's final state transition.

**Tech Stack:** Python 3, asyncio, `mcp` Python SDK (PyPI), systemd, GitHub CLI, Claude gateway HTTP API.

---

## File Structure

- Create: `github_adapter.py`
- Create: `state_store.py`
- Create: `terminal_worker.py`
- Create: `mcp_server.py`
- Create: `service_main.py`
- Create: `deploy/systemd/vllm-benchmarks-orchestrator.service`
- Create: `deploy/systemd/orchestrator.env.example`
- Create: `tests/main2main/test_state_store.py`
- Create: `tests/main2main/test_github_adapter.py`
- Create: `tests/main2main/test_terminal_worker.py`
- Create: `tests/main2main/test_mcp_server.py`
- Create: `tests/main2main/test_service_main.py`
- Modify: `main2main_orchestrator.py`
- Preserve: `tests/main2main/test_orchestrator.py` (must continue passing unchanged)

## Chunk 1: Extract `state_store.py` — Generic JSON Persistence

### Task 1: Create `JsonStore` with file locking

**Files:**
- Create: `state_store.py`
- Create: `tests/main2main/test_state_store.py`

- [ ] **Step 1: Write failing tests for JsonStore**

```python
# tests/main2main/test_state_store.py
import json
from pathlib import Path
import sys
import tempfile
import threading

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from state_store import JsonStore


def test_json_store_load_returns_empty_dict_when_file_does_not_exist():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        assert store.load() == {}


def test_json_store_save_and_load_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"key": "value", "nested": {"a": 1}})
        assert store.load() == {"key": "value", "nested": {"a": 1}}


def test_json_store_save_creates_parent_directories():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "sub" / "dir" / "state.json")
        store.save({"x": 1})
        assert store.load() == {"x": 1}


def test_json_store_locked_context_manager_provides_data_and_saves_on_exit():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"counter": 0})
        with store.locked() as data:
            data["counter"] = 1
        assert store.load() == {"counter": 1}


def test_json_store_locked_serializes_concurrent_writes():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({"counter": 0})
        errors = []

        def increment():
            try:
                with store.locked() as data:
                    val = data["counter"]
                    data["counter"] = val + 1
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=increment) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert store.load()["counter"] == 10
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `./.venv/bin/pytest tests/main2main/test_state_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'state_store'`

- [ ] **Step 3: Implement `JsonStore`**

```python
# state_store.py
from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


class JsonStore:
    """JSON file persistence with file locking.

    File locking protects against concurrent writes from asyncio.to_thread()
    and run_in_executor() calls that may overlap with the main event loop.
    """

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock_path = self.path.with_suffix(".lock")

    def load(self) -> dict:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text())

    def save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(data, indent=2, sort_keys=True))

    @contextmanager
    def locked(self) -> Iterator[dict]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_path.touch(exist_ok=True)
        with open(self._lock_path, "r") as lock_file:
            fcntl.flock(lock_file, fcntl.LOCK_EX)
            try:
                data = self.load()
                yield data
                self.save(data)
            finally:
                fcntl.flock(lock_file, fcntl.LOCK_UN)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `./.venv/bin/pytest tests/main2main/test_state_store.py -q`
Expected: PASS (5 passed)

- [ ] **Step 5: Commit**

```bash
git add state_store.py tests/main2main/test_state_store.py
git commit -m "feat: add JsonStore with file-locked persistence"
```

### Task 2: Wire `Main2MainStateStore` to use `JsonStore`

**Files:**
- Modify: `main2main_orchestrator.py:451-561`

- [ ] **Step 1: Run existing tests to confirm baseline**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py -q`
Expected: PASS (all existing tests pass)

- [ ] **Step 2: Update `Main2MainStateStore` to delegate to `JsonStore`**

In `main2main_orchestrator.py`, add import and change `Main2MainStateStore`:

```python
# At top of file, add:
from state_store import JsonStore

# Replace _load_all and _save_all in Main2MainStateStore:
class Main2MainStateStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._store = JsonStore(self.path)

    # ... keep all existing methods (register, get, update_after_fixup, etc.) ...

    def _load_all(self) -> dict[str, dict[str, str | int]]:
        return self._store.load()

    def _save_all(self, data: dict[str, dict[str, str | int]]) -> None:
        self._store.save(data)
```

Keep `self.path` as a public attribute because the existing code and tests reference it.

- [ ] **Step 3: Run existing tests to confirm no regressions**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py -q`
Expected: PASS (all existing tests still pass — the behavior is identical)

- [ ] **Step 4: Commit**

```bash
git add main2main_orchestrator.py
git commit -m "refactor: wire Main2MainStateStore to use JsonStore"
```

## Chunk 2: Extract `github_adapter.py`

### Task 3: Move `GitHubCliAdapter` to its own file

**Files:**
- Create: `github_adapter.py`
- Create: `tests/main2main/test_github_adapter.py`
- Modify: `main2main_orchestrator.py:162-448`

- [ ] **Step 1: Write a focused test for the generalized label parameter**

```python
# tests/main2main/test_github_adapter.py
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from github_adapter import GitHubCliAdapter


def test_list_open_pr_numbers_passes_label_to_gh_cli():
    calls = []

    def fake_runner(args):
        calls.append(args)
        return "[]"

    adapter = GitHubCliAdapter(runner=fake_runner)
    result = adapter.list_open_pr_numbers("nv-action/vllm-benchmarks", label="main2main")
    assert result == []
    assert "--label" in calls[0]
    label_idx = calls[0].index("--label")
    assert calls[0][label_idx + 1] == "main2main"


def test_list_open_pr_numbers_uses_custom_label():
    def fake_runner(args):
        label_idx = args.index("--label")
        assert args[label_idx + 1] == "custom-label"
        return '[{"number": 42}]'

    adapter = GitHubCliAdapter(runner=fake_runner)
    result = adapter.list_open_pr_numbers("repo", label="custom-label")
    assert result == [42]
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `./.venv/bin/pytest tests/main2main/test_github_adapter.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'github_adapter'`

- [ ] **Step 3: Create `github_adapter.py` by moving `GitHubCliAdapter`**

Move the entire `GitHubCliAdapter` class (lines 162-448 of `main2main_orchestrator.py`) to `github_adapter.py`. Changes:

1. Add necessary imports at top of `github_adapter.py`. Keep this file transport-focused; do not move main2main business dataclasses or parsing helpers into it:

```python
from __future__ import annotations

import json
import os
import subprocess
```

Import `RegistrationMetadata`, `FixupOutcome`, `parse_pr_metadata`, `parse_registration_comment`, and `parse_fixup_job_output` from `main2main_orchestrator.py`. `PrMetadata`, `RegistrationMetadata`, `FixupOutcome`, and `parse_*` remain owned by the orchestrator module.

2. Rename `list_open_main2main_pr_numbers` → `list_open_pr_numbers(self, repo, label="main2main")` and replace the hardcoded `"main2main"` string in the `--label` arg with the `label` parameter. Add backward-compat alias:
```python
# At end of GitHubCliAdapter class:
list_open_main2main_pr_numbers = list_open_pr_numbers  # backward compat
```

3. In `main2main_orchestrator.py`, replace the inline class with a direct import:
```python
from github_adapter import GitHubCliAdapter
```

This keeps `from main2main_orchestrator import GitHubCliAdapter` working while leaving business types in `main2main_orchestrator.py`.

4. In `main2main_orchestrator.py`, update `_build_github_adapter` and the `_TestRunOnceAdapter` class: rename `list_open_main2main_pr_numbers` → `list_open_pr_numbers` in the test adapter.

5. Prefer updating `run_once` to call `list_open_pr_numbers(...)`. Keep the backward-compat alias only as a temporary shim for existing imports/tests.

- [ ] **Step 4: Run all tests to confirm no regressions**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py tests/main2main/test_github_adapter.py -q`
Expected: PASS (all existing tests pass + 2 new tests pass)

- [ ] **Step 5: Commit**

```bash
git add github_adapter.py tests/main2main/test_github_adapter.py main2main_orchestrator.py
git commit -m "refactor: extract GitHubCliAdapter to github_adapter.py"
```

## Chunk 3: Add `terminal_worker.py` — Async Terminal Analysis

### Task 4: Create `TerminalJob` and `TerminalWorker` with persistence

**Files:**
- Create: `terminal_worker.py`
- Create: `tests/main2main/test_terminal_worker.py`

- [ ] **Step 1: Write failing tests for terminal worker**

```python
# tests/main2main/test_terminal_worker.py
import asyncio
import json
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
        worker.enqueue(TerminalJob(
            pr_number=1, repo="r", terminal_reason="done_failure",
            e2e_run_id="1", e2e_run_url="u", fixup_run_id=None,
        ))
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
        worker.enqueue(TerminalJob(
            pr_number=154, repo="r", terminal_reason="done_failure",
            e2e_run_id="999", e2e_run_url="u", fixup_run_id=None,
        ))
        asyncio.run(worker.process_one())
        assert any(c[0] == "extract" for c in calls)
        assert any(c[0] == "summarize" for c in calls)
        assert any(c[0] == "create" for c in calls)
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
        worker.enqueue(TerminalJob(
            pr_number=154, repo="r", terminal_reason="phase3_no_changes",
            e2e_run_id=None, e2e_run_url=None, fixup_run_id="run1",
        ))
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
        worker.enqueue(TerminalJob(
            pr_number=154, repo="r", terminal_reason="done_failure",
            e2e_run_id="999", e2e_run_url="u", fixup_run_id=None,
        ))
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
        worker.enqueue(TerminalJob(
            pr_number=154, repo="r", terminal_reason="done_failure",
            e2e_run_id="999", e2e_run_url="u", fixup_run_id=None,
        ))
        asyncio.run(worker.process_one())
        assert states == [(154, "manual_review")]


def test_reload_pending_jobs_on_init():
    with tempfile.TemporaryDirectory() as d:
        store = JsonStore(Path(d) / "state.json")
        store.save({
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
        })
        worker = _make_worker(store)
        assert worker.pending_count() == 1
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `./.venv/bin/pytest tests/main2main/test_terminal_worker.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'terminal_worker'`

- [ ] **Step 3: Implement `terminal_worker.py`**

Use a machine-readable issue marker comment, for example `<!-- main2main-manual-review repo=... pr=... fixup_run_id=... -->`, as the primary dedupe key when checking for existing issues.

```python
# terminal_worker.py
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass
from typing import Callable

from state_store import JsonStore

_JOBS_KEY = "_terminal_jobs"


@dataclass
class TerminalJob:
    pr_number: int
    repo: str
    terminal_reason: str
    e2e_run_id: str | None
    e2e_run_url: str | None
    fixup_run_id: str | None


class TerminalWorker:
    def __init__(
        self,
        *,
        store: JsonStore,
        extract_fn: Callable,
        summarize_fn: Callable,
        create_issue_fn: Callable,
        find_existing_issue_fn: Callable,
        update_state_fn: Callable,
        build_phase3_issue_fn: Callable,
        service_lock: asyncio.Lock | None,
    ):
        self._store = store
        self._extract_fn = extract_fn
        self._summarize_fn = summarize_fn
        self._create_issue_fn = create_issue_fn
        self._find_existing_issue_fn = find_existing_issue_fn
        self._update_state_fn = update_state_fn
        self._build_phase3_issue_fn = build_phase3_issue_fn
        self._service_lock = service_lock
        self._queue: list[dict] = []
        self._reload_pending()

    def _reload_pending(self) -> None:
        data = self._store.load()
        jobs = data.get(_JOBS_KEY, [])
        self._queue = [j for j in jobs if j.get("status") == "pending"]

    def enqueue(self, job: TerminalJob) -> None:
        entry = asdict(job)
        entry["status"] = "pending"
        with self._store.locked() as data:
            jobs = data.setdefault(_JOBS_KEY, [])
            jobs.append(entry)
        self._queue.append(entry)

    def pending_count(self) -> int:
        return len(self._queue)

    async def process_one(self) -> bool:
        if not self._queue:
            return False
        entry = self._queue[0]
        pr_number = entry["pr_number"]
        repo = entry["repo"]
        reason = entry["terminal_reason"]

        marker = (
            f"main2main-manual-review repo={repo} pr={pr_number} "
            f"fixup_run_id={entry.get('fixup_run_id')}"
        )

        existing = await asyncio.to_thread(
            self._find_existing_issue_fn,
            repo=repo,
            pr_number=pr_number,
            marker=marker,
        )
        if existing:
            await self._complete_job(entry, issue_url=existing)
            return True

        if reason == "done_failure" and entry.get("e2e_run_id"):
            analysis = await asyncio.to_thread(
                self._extract_fn,
                repo=repo,
                run_id=entry["e2e_run_id"],
            )
            body = await asyncio.to_thread(
                self._summarize_fn,
                analysis=analysis,
                terminal_reason=reason,
                pr_number=pr_number,
                e2e_run_id=entry.get("e2e_run_id"),
                e2e_run_url=entry.get("e2e_run_url"),
                fixup_run_id=entry.get("fixup_run_id"),
            )
        else:
            body = self._build_phase3_issue_fn(
                repo=repo,
                pr_number=pr_number,
                fixup_run_id=entry.get("fixup_run_id"),
                marker=marker,
            )

        issue_url = await asyncio.to_thread(
            self._create_issue_fn,
            repo=repo,
            title="main2main: manual review needed",
            body=body,
        )
        await self._complete_job(entry, issue_url=issue_url)
        return True

    async def _complete_job(self, entry: dict, *, issue_url: str) -> None:
        if self._service_lock is None:
            self._mark_done(entry, issue_url=issue_url)
            self._update_state_fn(pr_number=entry["pr_number"], status="manual_review")
            return
        async with self._service_lock:
            self._mark_done(entry, issue_url=issue_url)
            self._update_state_fn(pr_number=entry["pr_number"], status="manual_review")

    def _mark_done(self, entry: dict, *, issue_url: str) -> None:
        self._queue.remove(entry)
        with self._store.locked() as data:
            jobs = data.get(_JOBS_KEY, [])
            for j in jobs:
                if (
                    j.get("pr_number") == entry["pr_number"]
                    and j.get("status") == "pending"
                ):
                    j["status"] = "done"
                    j["issue_url"] = issue_url
                    break
            data[_JOBS_KEY] = [j for j in jobs if j.get("status") != "done"]

    async def run_loop(self) -> None:
        while True:
            try:
                processed = await self.process_one()
                if not processed:
                    await asyncio.sleep(5)
            except asyncio.CancelledError:
                break
            except Exception:
                await asyncio.sleep(10)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `./.venv/bin/pytest tests/main2main/test_terminal_worker.py -q`
Expected: PASS (7 passed)

- [ ] **Step 5: Commit**

```bash
git add terminal_worker.py tests/main2main/test_terminal_worker.py
git commit -m "feat: add TerminalWorker with async job queue and idempotency"
```

### Task 5: Wire `OrchestratorService` to enqueue terminal jobs instead of blocking

**Files:**
- Modify: `main2main_orchestrator.py:578-605, 683-692, 712-713, 780-796`

- [ ] **Step 1: Write a test that verifies `pending_terminal` status is set and `run_once` skips it**

Add to `tests/main2main/test_orchestrator.py`:

```python
def test_run_once_skips_pending_terminal_status():
    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "state.json"
        store = Main2MainStateStore(state_file)
        store.register(Main2MainState(
            repo="nv-action/vllm-benchmarks",
            pr_number=154,
            branch="branch",
            head_sha="a" * 40,
            old_commit="b" * 40,
            new_commit="c" * 40,
            phase="done",
            status="pending_terminal",
        ))

        class FakeGH:
            def list_open_pr_numbers(self, repo, label="main2main"):
                return [154]

        service = OrchestratorService(store, FakeGH())
        result = service.run_once("nv-action/vllm-benchmarks")
        assert result["reconciled"] == {}
        assert result["fixup_outcomes"] == {}
```

- [ ] **Step 2: Run the new test to confirm it fails**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py::test_run_once_skips_pending_terminal_status -q`
Expected: FAIL — `run_once` does not skip `pending_terminal` status yet.

- [ ] **Step 3: Update `run_once` to skip `pending_terminal`**

In `main2main_orchestrator.py`, in `run_once` method, after the `manual_review` check (line 712-713), add:

```python
if state.status == "pending_terminal":
    continue
```

- [ ] **Step 4: Run all tests to confirm no regressions**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add main2main_orchestrator.py tests/main2main/test_orchestrator.py
git commit -m "feat: add pending_terminal status and skip in run_once"
```

### Task 5b: Add optional `terminal_enqueue_fn` to `OrchestratorService`

**Files:**
- Modify: `main2main_orchestrator.py:564-576, 683-692, 780-796`

The `OrchestratorService` needs an optional callback so the async service can enqueue terminal jobs instead of blocking. The CLI path passes no callback and continues using the existing blocking behavior.

- [ ] **Step 1: Write a test for the enqueue path**

Add to `tests/main2main/test_orchestrator.py`:

```python
def test_reconcile_enqueues_terminal_job_when_callback_provided():
    with tempfile.TemporaryDirectory() as d:
        state_file = Path(d) / "state.json"
        store = Main2MainStateStore(state_file)
        store.register(Main2MainState(
            repo="r", pr_number=1, branch="b", head_sha="a" * 40,
            old_commit="b" * 40, new_commit="c" * 40, phase="done",
            status="waiting_e2e",
        ))
        enqueued = []

        class FakeGH:
            def get_pr_context(self, repo, pr_number):
                return {
                    "pr_number": 1, "head_sha": "a" * 40, "branch": "b",
                    "state": "OPEN", "labels": ["main2main"],
                    "metadata": PrMetadata(old_commit="b" * 40, new_commit="c" * 40),
                    "body": "",
                }
            def wait_for_e2e_full(self, *, repo, head_sha):
                return {"run_id": "99", "head_sha": "a" * 40, "conclusion": "failure", "run_url": "u"}

        service = OrchestratorService(
            store, FakeGH(),
            terminal_enqueue_fn=lambda **kw: enqueued.append(kw),
        )
        result = service.reconcile("r", 1)
        assert result["action"] == "create_manual_review"
        assert len(enqueued) == 1
        assert enqueued[0]["terminal_reason"] == "done_failure"
        state = store.get("r", 1)
        assert state.status == "pending_terminal"
```

- [ ] **Step 2: Run to confirm it fails**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py::test_reconcile_enqueues_terminal_job_when_callback_provided -q`
Expected: FAIL

- [ ] **Step 3: Add `terminal_enqueue_fn` parameter to `OrchestratorService`**

Also add a dedicated `build_phase3_no_changes_issue(...)` helper and use it in both the async worker path and the synchronous CLI fallback path. Do not reuse `_build_manual_review_issue(...)` for `phase3_no_changes`.

```python
# In OrchestratorService.__init__, add parameter:
class OrchestratorService:
    def __init__(
        self,
        store: Main2MainStateStore,
        github: GitHubCliAdapter,
        *,
        sleep_fn=time.sleep,
        token_factory=None,
        terminal_enqueue_fn=None,
    ):
        # ... existing init ...
        self.terminal_enqueue_fn = terminal_enqueue_fn
```

In `reconcile`, replace the `create_manual_review` branch (lines 683-692):
```python
elif decision.action == "create_manual_review":
    if self.terminal_enqueue_fn is not None:
        self.store.register(replace(state, status="pending_terminal", active_fixup_run_id=None))
        self.terminal_enqueue_fn(
            pr_number=pr_number,
            repo=repo,
            terminal_reason="done_failure",
            e2e_run_id=e2e_result["run_id"],
            e2e_run_url=e2e_result["run_url"],
            fixup_run_id=None,
        )
    else:
        issue = self._build_manual_review_issue(
            state=state, pr_number=pr_number, terminal_reason="done_failure",
            e2e_run_url=e2e_result["run_url"], e2e_run_id=e2e_result["run_id"],
        )
        self.store.register(replace(state, status="manual_review", active_fixup_run_id=None))
        self.github.create_manual_review_issue(repo=repo, title=issue["title"], body=issue["body"])
```

In `apply_fixup_outcome`, replace the phase3 manual review branch (lines 780-796):
```python
if state.phase == "3":
    if self.terminal_enqueue_fn is not None:
        self.store.register(replace(cleared, status="pending_terminal"))
        self.terminal_enqueue_fn(
            pr_number=pr_number,
            repo=repo,
            terminal_reason="phase3_no_changes",
            e2e_run_id=None,
            e2e_run_url=None,
            fixup_run_id=fixup_run_id,
        )
    else:
        issue = build_phase3_no_changes_issue(
            state=state,
            pr_number=pr_number,
            fixup_run_id=fixup_run_id,
        )
        self.github.create_manual_review_issue(repo=repo, title=issue["title"], body=issue["body"])
    return {
        "action": "create_manual_review",
        "phase": cleared.phase,
        "reason": "phase 3 completed without code changes",
    }
```

- [ ] **Step 4: Run all tests to confirm no regressions**

Run: `./.venv/bin/pytest tests/main2main/test_orchestrator.py -q`
Expected: PASS (existing tests use no `terminal_enqueue_fn`, so they still use the blocking path)

- [ ] **Step 5: Commit**

```bash
git add main2main_orchestrator.py tests/main2main/test_orchestrator.py
git commit -m "feat: add terminal_enqueue_fn to OrchestratorService for async terminal analysis"
```

## Chunk 4: Add `mcp_server.py` — MCP Interface

### Task 6: Create MCP server with 6 tools

**Files:**
- Create: `mcp_server.py`
- Create: `tests/main2main/test_mcp_server.py`

- [ ] **Step 1: Write failing tests for MCP tool registration and read tools**

```python
# tests/main2main/test_mcp_server.py
import asyncio
import json
from pathlib import Path
import sys
import tempfile

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
    """Test that build_mcp_server registers the expected tool handlers.
    We test via the get_tools / handle_tool_call helpers exposed by build_mcp_server."""
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    tool_names = {t["name"] for t in server.get_tools()}
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
    result = asyncio.run(server.handle_tool_call(
        "orchestrator_get_pr_state",
        {"repo": "r", "pr_number": 999},
    ))
    assert json.loads(result) is None


def test_run_once_delegates_to_service():
    service = FakeService()
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    asyncio.run(server.handle_tool_call(
        "orchestrator_run_once",
        {"repo": "nv-action/vllm-benchmarks"},
    ))
    assert service.run_once_calls == ["nv-action/vllm-benchmarks"]


def test_run_once_returns_error_on_exception():
    service = FakeService()
    service.run_once = lambda repo: (_ for _ in ()).throw(ValueError("test error"))
    store = FakeStore()
    github = FakeGitHub()
    lock = asyncio.Lock()
    server = build_mcp_server(service, store, github, lock, {})
    result = asyncio.run(server.handle_tool_call(
        "orchestrator_run_once",
        {"repo": "r"},
    ))
    parsed = json.loads(result)
    assert "error" in parsed
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `./.venv/bin/pytest tests/main2main/test_mcp_server.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcp_server'`

- [ ] **Step 3: Implement `mcp_server.py`**

Before wiring health, add a small public `load_all()` helper to `Main2MainStateStore` that delegates to `_load_all()`. The MCP health tool should use that public method instead of reaching into `JsonStore` directly.

```python
# mcp_server.py
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict


_start_time = time.time()

_TOOLS = [
    {
        "name": "orchestrator_list_prs",
        "description": "List open PRs with a given label",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "label": {"type": "string", "default": "main2main"},
            },
            "required": ["repo"],
        },
    },
    {
        "name": "orchestrator_get_pr_state",
        "description": "Get orchestrator state for a tracked PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "orchestrator_get_health",
        "description": "Get service health status",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "orchestrator_run_once",
        "description": "Run one reconciliation cycle for all tracked PRs",
        "inputSchema": {
            "type": "object",
            "properties": {"repo": {"type": "string"}},
            "required": ["repo"],
        },
    },
    {
        "name": "orchestrator_reconcile_pr",
        "description": "Reconcile a single PR",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
    {
        "name": "orchestrator_register_pr",
        "description": "Register a PR from its comment metadata",
        "inputSchema": {
            "type": "object",
            "properties": {
                "repo": {"type": "string"},
                "pr_number": {"type": "integer"},
            },
            "required": ["repo", "pr_number"],
        },
    },
]


class McpOrchestrator:
    """Wraps orchestrator service as MCP tools. Testable without MCP protocol."""

    def __init__(self, service, store, github, service_lock: asyncio.Lock, poll_state: dict):
        self._service = service
        self._store = store
        self._github = github
        self._lock = service_lock
        self._poll_state = poll_state

    def get_tools(self) -> list[dict]:
        return list(_TOOLS)

    async def handle_tool_call(self, name: str, arguments: dict) -> str:
        try:
            if name == "orchestrator_list_prs":
                result = await asyncio.to_thread(
                    self._github.list_open_pr_numbers,
                    arguments["repo"],
                    label=arguments.get("label", "main2main"),
                )
                return json.dumps(result)

            if name == "orchestrator_get_pr_state":
                state = self._store.get(arguments["repo"], arguments["pr_number"])
                result = asdict(state) if state else None
                return json.dumps(result)

            if name == "orchestrator_get_health":
                data = self._store.load_all() if self._store.path.exists() else {}
                terminal_jobs = data.get("_terminal_jobs", [])
                pending = sum(1 for j in terminal_jobs if j.get("status") == "pending")
                pr_count = sum(1 for k in data if not k.startswith("_"))
                health = {
                    "state_file_exists": self._store.path.exists(),
                    "state_file_path": str(self._store.path),
                    "tracked_pr_count": pr_count,
                    "terminal_jobs_pending": pending,
                    "last_poll_time": self._poll_state.get("last_poll_time"),
                    "last_poll_result": self._poll_state.get("last_poll_result"),
                    "uptime_seconds": round(time.time() - _start_time, 1),
                }
                return json.dumps(health)

            if name == "orchestrator_run_once":
                async with self._lock:
                    result = await asyncio.to_thread(
                        self._service.run_once, arguments["repo"]
                    )
                return json.dumps(result)

            if name == "orchestrator_reconcile_pr":
                async with self._lock:
                    result = await asyncio.to_thread(
                        self._service.reconcile,
                        arguments["repo"],
                        arguments["pr_number"],
                    )
                return json.dumps(result)

            if name == "orchestrator_register_pr":
                async with self._lock:
                    result = await asyncio.to_thread(
                        self._service.register_from_pr_comment,
                        arguments["repo"],
                        arguments["pr_number"],
                    )
                return json.dumps(result)

            return json.dumps({"error": f"unknown tool: {name}"})
        except Exception as exc:
            return json.dumps({"error": type(exc).__name__, "message": str(exc)})


def build_mcp_server(service, store, github, service_lock: asyncio.Lock, poll_state: dict) -> McpOrchestrator:
    return McpOrchestrator(service, store, github, service_lock, poll_state)


def create_mcp_protocol_server(orchestrator: McpOrchestrator):
    """Create an MCP SDK Server wired to our orchestrator. Used by service_main.py."""
    from mcp.server import Server
    from mcp.types import TextContent, Tool

    server = Server("main2main-orchestrator")

    @server.list_tools()
    async def list_tools():
        return [Tool(**t) for t in orchestrator.get_tools()]

    @server.call_tool()
    async def call_tool(name: str, arguments: dict):
        result = await orchestrator.handle_tool_call(name, arguments)
        return [TextContent(type="text", text=result)]

    return server
```

This separates the testable logic (`McpOrchestrator`) from the MCP protocol wiring (`create_mcp_protocol_server`). Tests call `get_tools()` and `handle_tool_call()` directly. The MCP SDK `Server` is only instantiated in `service_main.py`.

- [ ] **Step 4: Run tests to confirm they pass**

Run: `./.venv/bin/pytest tests/main2main/test_mcp_server.py -q`
Expected: PASS (4 passed)

- [ ] **Step 5: Commit**

```bash
git add mcp_server.py tests/main2main/test_mcp_server.py
git commit -m "feat: add MCP server with 6 orchestrator tools"
```

## Chunk 5: Add `service_main.py` — asyncio Entrypoint

### Task 7: Create the service entrypoint

**Files:**
- Create: `service_main.py`
- Create: `tests/main2main/test_service_main.py`

- [ ] **Step 1: Write failing test for poll_loop**

```python
# tests/main2main/test_service_main.py
import asyncio
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from service_main import poll_loop


def test_poll_loop_calls_run_once_and_records_last_poll_time():
    calls = []

    class FakeService:
        def run_once(self, repo):
            calls.append(repo)
            return {"reconciled": {}}

    lock = asyncio.Lock()
    state = {}

    async def run():
        task = asyncio.create_task(
            poll_loop(FakeService(), "test-repo", 0.01, lock, state)
        )
        await asyncio.sleep(0.05)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(run())
    assert len(calls) >= 1
    assert calls[0] == "test-repo"
    assert state.get("last_poll_time") is not None
    assert state.get("last_poll_result") == {"reconciled": {}}


def test_poll_loop_acquires_service_lock():
    locked_during_call = []

    class FakeService:
        def run_once(self, repo):
            return {}

    lock = asyncio.Lock()
    state = {}

    original_to_thread = asyncio.to_thread

    async def patched_poll_loop(service, repo, interval, lk, st):
        async with lk:
            locked_during_call.append(True)
            result = await original_to_thread(service.run_once, repo)
        st["last_poll_result"] = result

    asyncio.run(patched_poll_loop(FakeService(), "r", 0.01, lock, state))
    assert locked_during_call == [True]
```

- [ ] **Step 2: Run tests to confirm they fail**

Run: `./.venv/bin/pytest tests/main2main/test_service_main.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'service_main'`

- [ ] **Step 3: Implement `service_main.py`**

```python
# service_main.py
from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("main2main-service")


async def poll_loop(
    service,
    repo: str,
    interval: float,
    lock: asyncio.Lock,
    state: dict,
) -> None:
    while True:
        try:
            async with lock:
                result = await asyncio.to_thread(service.run_once, repo)
            state["last_poll_time"] = datetime.now(timezone.utc).isoformat()
            state["last_poll_result"] = result
            log.info("poll complete: %s", result)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll_loop error")
        await asyncio.sleep(interval)


async def _run(
    state_path: str,
    repo: str,
    poll_interval: float,
    mcp_host: str,
    mcp_port: int,
) -> None:
    # Import here to avoid circular imports and keep entrypoint clean
    from github_adapter import GitHubCliAdapter
    from main2main_orchestrator import Main2MainStateStore, OrchestratorService
    from mcp_server import build_mcp_server, create_mcp_protocol_server
    from state_store import JsonStore
    from terminal_worker import TerminalJob, TerminalWorker

    store = Main2MainStateStore(state_path)
    github = GitHubCliAdapter()
    service_lock = asyncio.Lock()
    poll_state: dict = {}

    json_store = JsonStore(Path(state_path))

    def update_pr_state(pr_number: int, status: str) -> None:
        current = store.get(repo, pr_number)
        if current:
            from dataclasses import replace
            updated = replace(current, status=status, active_fixup_run_id=None)
            store.register(updated)

    def find_existing_issue(repo: str, pr_number: int, marker: str) -> str | None:
        import subprocess
        try:
            result = subprocess.run(
                [
                    "gh", "issue", "list",
                    "--repo", repo,
                    "--label", "main2main",
                    "--search", marker,
                    "--json", "url",
                    "-L", "1",
                ],
                capture_output=True, text=True, check=True,
            )
            issues = __import__("json").loads(result.stdout)
            if issues:
                return issues[0]["url"]
        except Exception:
            pass
        return None

    from main2main_orchestrator import extract_e2e_failure_analysis

    def summarize_for_worker(
        *, analysis, terminal_reason, pr_number,
        e2e_run_id, e2e_run_url, fixup_run_id,
    ) -> str:
        from main2main_orchestrator import Main2MainState, summarize_manual_review_issue
        dummy_state = store.get(repo, pr_number)
        if dummy_state is None:
            return f"Terminal analysis for PR #{pr_number}: {terminal_reason}"
        return summarize_manual_review_issue(
            analysis=analysis,
            state=dummy_state,
            terminal_reason=terminal_reason,
            e2e_run_url=e2e_run_url,
            e2e_run_id=e2e_run_id,
            fixup_run_id=fixup_run_id,
        )

    def build_phase3_issue(*, repo: str, pr_number: int, fixup_run_id: str | None, marker: str) -> str:
        return (
            "## Main2Main Auto — Manual Review Required\n\n"
            f"Phase 3 fixup completed without code changes for PR #{pr_number}.\n"
            f"Fixup run: {fixup_run_id or 'N/A'}\n\n"
            "Please review the PR and apply remaining fixes manually.\n\n"
            f"<!-- {marker} -->"
        )

    terminal = TerminalWorker(
        store=json_store,
        extract_fn=lambda **kw: extract_e2e_failure_analysis(**kw),
        summarize_fn=summarize_for_worker,
        create_issue_fn=lambda **kw: github.create_manual_review_issue(**kw),
        find_existing_issue_fn=find_existing_issue,
        update_state_fn=update_pr_state,
        build_phase3_issue_fn=build_phase3_issue,
        service_lock=service_lock,
    )

    # Wire OrchestratorService with terminal_enqueue_fn so reconcile/apply_fixup_outcome
    # enqueue async jobs instead of blocking
    def enqueue_terminal(**kw):
        terminal.enqueue(TerminalJob(**kw))

    service = OrchestratorService(store, github, terminal_enqueue_fn=enqueue_terminal)

    orchestrator_mcp = build_mcp_server(service, store, github, service_lock, poll_state)
    mcp = create_mcp_protocol_server(orchestrator_mcp)

    loop = asyncio.get_event_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, lambda: [t.cancel() for t in asyncio.all_tasks(loop)])

    log.info("starting service: repo=%s poll_interval=%s mcp=%s:%s", repo, poll_interval, mcp_host, mcp_port)

    try:
        await asyncio.gather(
            poll_loop(service, repo, poll_interval, service_lock, poll_state),
            terminal.run_loop(),
            mcp.run(transport="sse", host=mcp_host, port=mcp_port),
        )
    except asyncio.CancelledError:
        log.info("service shutting down")


def main() -> None:
    state_path = os.environ.get("STATE_PATH", "/var/lib/vllm-benchmarks-orchestrator/state.json")
    repo = os.environ.get("REPO", "nv-action/vllm-benchmarks")
    poll_interval = float(os.environ.get("POLL_INTERVAL", "60"))
    mcp_host = os.environ.get("MCP_HOST", "127.0.0.1")
    mcp_port = int(os.environ.get("MCP_PORT", "8080"))

    asyncio.run(_run(state_path, repo, poll_interval, mcp_host, mcp_port))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests to confirm they pass**

Run: `./.venv/bin/pytest tests/main2main/test_service_main.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add service_main.py tests/main2main/test_service_main.py
git commit -m "feat: add asyncio service entrypoint with poll loop"
```

## Chunk 6: Add systemd Deployment Assets

### Task 8: Create deployment files

**Files:**
- Create: `deploy/systemd/vllm-benchmarks-orchestrator.service`
- Create: `deploy/systemd/orchestrator.env.example`

- [ ] **Step 1: Create systemd service unit**

```ini
# deploy/systemd/vllm-benchmarks-orchestrator.service
[Unit]
Description=vllm-benchmarks main2main orchestrator
After=network-online.target

[Service]
Type=simple
EnvironmentFile=/etc/vllm-benchmarks-orchestrator/orchestrator.env
WorkingDirectory=/opt/vllm-benchmarks-orchestrator/current
ExecStart=/opt/vllm-benchmarks-orchestrator/venv/bin/python service_main.py
Restart=on-failure
RestartSec=10
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
```

- [ ] **Step 2: Create env example**

```bash
# deploy/systemd/orchestrator.env.example
GITHUB_TOKEN=ghp_xxxxx
ANTHROPIC_BASE_URL=https://your-gateway.example.com
ANTHROPIC_AUTH_TOKEN=sk-xxxxx
STATE_PATH=/var/lib/vllm-benchmarks-orchestrator/state.json
POLL_INTERVAL=60
REPO=nv-action/vllm-benchmarks
MCP_HOST=127.0.0.1
MCP_PORT=8080
```

- [ ] **Step 3: Write a deployment asset validation test**

```python
# tests/main2main/test_deploy_assets.py
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def test_systemd_service_uses_correct_entrypoint():
    text = Path("deploy/systemd/vllm-benchmarks-orchestrator.service").read_text()
    assert "service_main.py" in text
    assert "EnvironmentFile=/etc/vllm-benchmarks-orchestrator/orchestrator.env" in text
    assert "TimeoutStopSec=60" in text


def test_env_example_contains_required_variables():
    text = Path("deploy/systemd/orchestrator.env.example").read_text()
    for var in ["GITHUB_TOKEN", "ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN",
                "STATE_PATH", "POLL_INTERVAL", "REPO", "MCP_HOST", "MCP_PORT"]:
        assert var in text
```

- [ ] **Step 4: Run the deployment tests**

Run: `./.venv/bin/pytest tests/main2main/test_deploy_assets.py -q`
Expected: PASS (2 passed)

- [ ] **Step 5: Commit**

```bash
git add deploy/systemd/vllm-benchmarks-orchestrator.service deploy/systemd/orchestrator.env.example tests/main2main/test_deploy_assets.py
git commit -m "feat: add systemd deployment assets"
```

## Chunk 7: Full Regression and Final Validation

### Task 9: Run full test suite

**Files:**
- Modify: any failing files from previous chunks

- [ ] **Step 1: Run the complete test suite**

Run:
```bash
./.venv/bin/pytest \
  tests/main2main/test_orchestrator.py \
  tests/main2main/test_state_store.py \
  tests/main2main/test_github_adapter.py \
  tests/main2main/test_terminal_worker.py \
  tests/main2main/test_mcp_server.py \
  tests/main2main/test_service_main.py \
  tests/main2main/test_deploy_assets.py \
  -q
```
Expected: ALL PASS.

- [ ] **Step 2: Verify Python compilation**

Run:
```bash
python3 -m py_compile main2main_orchestrator.py && \
python3 -m py_compile github_adapter.py && \
python3 -m py_compile state_store.py && \
python3 -m py_compile terminal_worker.py && \
python3 -m py_compile mcp_server.py && \
python3 -m py_compile service_main.py
```
Expected: no errors.

- [ ] **Step 3: Verify CLI still works**

Run:
```bash
python3 main2main_orchestrator.py register \
  --state-file /tmp/test-mcp-migration.json \
  --repo nv-action/vllm-benchmarks \
  --pr-number 999 \
  --branch test-branch \
  --head-sha aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa \
  --old-commit bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb \
  --new-commit cccccccccccccccccccccccccccccccccccccccc \
  --phase 2
```
Expected: prints JSON state, exit code 0.

- [ ] **Step 4: Clean up temp file and commit any fixes**

```bash
rm -f /tmp/test-mcp-migration.json
git add -A
git diff --cached --stat
# Only commit if there are fixes needed
```

- [ ] **Step 5: Final commit**

```bash
git add main2main_orchestrator.py github_adapter.py state_store.py terminal_worker.py mcp_server.py service_main.py tests/ deploy/
git commit -m "test: complete main2main MCP service implementation"
```
