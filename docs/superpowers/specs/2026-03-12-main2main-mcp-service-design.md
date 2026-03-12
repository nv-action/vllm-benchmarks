# Main2Main MCP Service Design

> Adds an MCP interface and long-lived service deployment to the existing `main2main_orchestrator.py`.

## Context

`main2main_orchestrator.py` (~1100 lines) is a working, tested orchestration state machine for the `main2main` PR automation flow. It has:

- `OrchestratorService` with `run_once`, `reconcile`, `run_loop`, `register_from_pr_comment`, `apply_fixup_outcome`
- `GitHubCliAdapter` wrapping all GitHub CLI interactions
- `Main2MainStateStore` for JSON-file-based state persistence
- A CLI interface supporting `register`, `decide`, `update-after-fixup`, `apply-fixup-outcome`, `register-from-pr-comment`, `run-once`, `run-loop`, `reconcile`

What it lacks:

- A long-lived service runtime (currently run manually via CLI)
- An MCP interface for operator interaction
- Async terminal analysis (currently blocks the reconcile loop for several minutes daily)

## Goal

Deploy the existing `main2main` orchestrator as a long-lived systemd service with:

1. An MCP server (SSE transport) exposing query and control tools
2. A background reconcile polling loop
3. An async terminal-analysis worker that does not block reconciliation
4. Minimal extraction of genuinely reusable layers into separate files

## Non-Goals

- Flow-policy abstraction layer (no second flow exists)
- Registration comment format migration
- Workflow terminal-action migration
- Package directory structure (`pr_orchestrator/` hierarchy)
- Cross-repository orchestration
- Web dashboard

## Design Approaches Considered

### Approach A: Single process, two threads

Reconcile loop in main thread, MCP server in a second thread.

Rejected: introduces thread-safety concerns for shared state with no benefit over asyncio.

### Approach B: Single process, asyncio with MCP as main loop

MCP server runs the asyncio event loop. Reconcile polling and terminal-analysis worker run as asyncio tasks in the same loop.

Selected: simplest deployment. Blocking control paths still run in worker threads via `asyncio.to_thread()`, but a shared `asyncio.Lock` serializes those control operations so business transactions remain effectively single-file.

### Approach C: Two independent processes

Separate worker and MCP server processes, coordinated via file lock.

Rejected: doubles deployment complexity, file-lock coordination is error-prone, no benefit at current scale.

## Architecture

Single asyncio process hosting three concurrent tasks:

```
asyncio event loop
  ├── MCP server (SSE on port 8080)
  ├── reconcile poller (run_once every N seconds)
  └── terminal-analysis worker (drains job queue)
```

All three tasks share in-process references to `OrchestratorService`, `StateStore`, and `GitHubAdapter`. Since control paths (`run_once`, `reconcile`, `register_from_pr_comment`, `apply_fixup_outcome`) are dispatched to threads via `asyncio.to_thread()`, they can overlap. A single `asyncio.Lock` (the "service lock") must be acquired before each `to_thread()` call on any control path. This ensures that at most one control operation executes at a time, preserving the read-decide-write atomicity of business transactions without requiring changes to the synchronous `OrchestratorService` internals.

MCP read tools and the terminal worker's issue-creation step do not acquire the service lock — reads are safe to overlap, and the terminal worker only writes to state after its heavy I/O is complete (at which point it acquires the lock for the final state update).

File locking in `JsonStore` remains as a defense-in-depth layer for the state file itself.

The process runs under systemd on a fixed Linux host. Claude Code connects via:

```bash
claude mcp add orchestrator --transport sse --url http://<host>:8080/sse
```

## Blocking I/O Strategy

`OrchestratorService` methods are synchronous — they call `subprocess.run` (GitHub CLI) and `urllib.request.urlopen` (Claude gateway). In the asyncio service, these are handled as follows:

- **Reconcile poller**: wraps `service.run_once()` in `asyncio.to_thread()` so it does not block the event loop.
- **MCP control tools** (`orchestrator_run_once`, `orchestrator_reconcile_pr`, `orchestrator_register_pr`): wrap the underlying service call in `asyncio.to_thread()`.
- **MCP read tools** (`orchestrator_list_prs`, `orchestrator_get_pr_state`, `orchestrator_get_health`): `get_pr_state` and `get_health` read local state (fast, no wrapping needed). `list_prs` calls GitHub CLI, so it uses `asyncio.to_thread()`.
- **Terminal worker**: uses `asyncio.to_thread()` for `extract_e2e_failure_analysis` and `summarize_manual_review_issue`.

No async rewrite of `OrchestratorService` is needed. The service remains synchronous and testable via its existing CLI and test suite.

## File Structure

Extract genuinely reusable layers from `main2main_orchestrator.py` into flat sibling files:

```
github_adapter.py      # GitHubCliAdapter with generalized method names
state_store.py         # Generic JSON persistence + file locking
terminal_worker.py     # Async job queue + terminal analysis worker
mcp_server.py          # MCP server exposing 6 tools (using `mcp` Python SDK)
service_main.py        # asyncio entrypoint: starts poller + MCP + terminal worker

main2main_orchestrator.py  # Retains business logic + CLI, imports from above

deploy/systemd/vllm-benchmarks-orchestrator.service
deploy/systemd/orchestrator.env.example
```

### What moves where

| Current location | Destination | What changes |
|---|---|---|
| `GitHubCliAdapter` class | `github_adapter.py` | Rename `list_open_main2main_pr_numbers` → `list_open_pr_numbers`; generalize label parameter |
| `Main2MainStateStore._load_all`, `_save_all`, file I/O | `state_store.py` | Extract generic `JsonStore` (load/save/lock); `Main2MainStateStore` stays in `main2main_orchestrator.py` and uses `JsonStore` |
| `_build_manual_review_issue`, `extract_e2e_failure_analysis`, `summarize_manual_review_issue` | `terminal_worker.py` | Wrap in async job queue; reconcile enqueues, worker drains |
| (new) | `mcp_server.py` | MCP tool definitions, imports `OrchestratorService` |
| (new) | `service_main.py` | asyncio entrypoint for systemd |
| Business logic, CLI | `main2main_orchestrator.py` (stays) | Imports extracted layers; CLI remains compatible |

### What stays in `main2main_orchestrator.py`

- `Main2MainState`, `PrMetadata`, `RegistrationMetadata`, `ActionDecision`, `FixupOutcome` dataclasses
- `Main2MainStateStore` (uses `JsonStore` from `state_store.py`)
- `OrchestratorService` (uses `GitHubCliAdapter` from `github_adapter.py`)
- `decide_next_action`, `apply_fixup_result`, `apply_no_change_fixup_result`, `parse_*` functions
- `_build_parser`, `_main` CLI entrypoint

## Module Details

### `github_adapter.py`

Move `GitHubCliAdapter` as-is, with these renames:

| Current method | New name | Reason |
|---|---|---|
| `list_open_main2main_pr_numbers` | `list_open_pr_numbers(repo, label)` | Label becomes a parameter |

All other methods (`get_pr_context`, `get_workflow_run`, `dispatch_fixup`, `create_manual_review_issue`, etc.) move unchanged.

`github_adapter.py` stays transport-focused. `PrMetadata`, `RegistrationMetadata`, `FixupOutcome`, and the `parse_*` helpers remain in `main2main_orchestrator.py` because they encode main2main-specific business semantics rather than generic GitHub access.

### `state_store.py`

Extract the generic persistence pattern:

```python
class JsonStore:
    """JSON file persistence with file locking.

    File locking protects against concurrent writes from asyncio.to_thread()
    and run_in_executor() calls that may overlap with the main event loop.
    """
    def __init__(self, path: Path): ...
    def load(self) -> dict: ...
    def save(self, data: dict) -> None: ...
    @contextmanager
    def locked(self) -> Iterator[dict]: ...
```

`Main2MainStateStore` in `main2main_orchestrator.py` changes from direct file I/O to using `JsonStore`:

```python
class Main2MainStateStore:
    def __init__(self, path):
        self._store = JsonStore(path)
    def _load_all(self):
        return self._store.load()
    def _save_all(self, data):
        self._store.save(data)
```

### `terminal_worker.py`

Async job queue for terminal analysis:

```python
class TerminalJob:
    pr_number: int
    repo: str
    terminal_reason: str  # "done_failure" or "phase3_no_changes"
    e2e_run_id: str | None
    e2e_run_url: str | None
    fixup_run_id: str | None

class TerminalWorker:
    def enqueue(self, job: TerminalJob) -> None: ...
    async def run_loop(self) -> None:
        """Drain queue, process one job at a time."""
    def pending_count(self) -> int: ...
```

`TerminalWorker` receives the shared `service_lock`. Heavy analysis and network I/O run without the lock; the final state transition and job-completion write reacquire the lock so worker completion cannot overlap with `run_once`, `reconcile`, `register_from_pr_comment`, or `apply_fixup_outcome`.

**Handling different terminal reasons:**

- `done_failure`: requires `e2e_run_id`. Worker calls `extract_e2e_failure_analysis` then `summarize_manual_review_issue`.
- `phase3_no_changes`: no E2E run to analyze. Worker skips `extract_e2e_failure_analysis` and uses a dedicated `build_phase3_no_changes_issue(...)` helper shared with the synchronous CLI fallback path.

The worker runs blocking calls in `asyncio.to_thread()`, then calls `github.create_manual_review_issue`.

Jobs are persisted to the state file so they survive process restart. On startup, the worker reloads pending jobs and resumes exactly once.

### `mcp_server.py`

Built using the `mcp` Python SDK (PyPI: `mcp`). Six MCP tools wrapping existing `OrchestratorService` methods:

| MCP Tool | Maps to | Read/Control |
|---|---|---|
| `orchestrator_list_prs` | `github.list_open_pr_numbers(repo, label="main2main")` | Read |
| `orchestrator_get_pr_state` | `store.get(repo, pr_number)` | Read |
| `orchestrator_get_health` | See health schema below | Read |
| `orchestrator_run_once` | `service.run_once(repo)` | Control |
| `orchestrator_reconcile_pr` | `service.reconcile(repo, pr_number)` | Control |
| `orchestrator_register_pr` | `service.register_from_pr_comment(repo, pr_number)` | Control |

Transport: SSE on a configurable port (default 8080). Default bind address: `127.0.0.1` (localhost only). Remote access should use SSH tunnel (`ssh -L 8080:localhost:8080 <host>`) or a reverse proxy with authentication. The MCP server does not implement its own authentication layer.

`build_mcp_server(...)` receives a shared `poll_state` dictionary populated by `poll_loop`. `orchestrator_get_health` reads poll timing and last result only from this injected state, not from module globals.

**Health schema:**

```python
{
    "state_file_exists": bool,
    "state_file_path": str,
    "tracked_pr_count": int,
    "terminal_jobs_pending": int,
    "last_poll_time": str | None,   # ISO 8601, held in-memory by the poller
    "last_poll_result": dict | None, # result of last run_once
    "uptime_seconds": float,
}
```

`last_poll_time` and `last_poll_result` live in the shared `poll_state` dictionary owned by `service_main.py` and passed into the MCP server. They are not persisted — if the process restarts, they reset to `None` until the first poll completes.

**Error handling:**

MCP tools return errors using the MCP SDK's standard error response mechanism. Specifically:
- Service exceptions (`KeyError`, `ValueError`) → MCP error with the exception message
- GitHub CLI failures (`subprocess.CalledProcessError`) → MCP error with stderr content
- State file missing/corrupt → MCP error with descriptive message

The MCP server does not crash on tool errors. Each tool call is independent.

### `service_main.py`

Single asyncio entrypoint for systemd:

```python
async def main():
    store = Main2MainStateStore(state_path)
    github = GitHubCliAdapter()
    service = OrchestratorService(store, github)
    service_lock = asyncio.Lock()  # serializes all control operations
    poll_state = {"last_poll_time": None, "last_poll_result": None}
    terminal = TerminalWorker(store, github, service, service_lock)
    mcp = build_mcp_server(service, store, github, service_lock, poll_state)

    await asyncio.gather(
        poll_loop(service, repo, interval, service_lock, poll_state),
        terminal.run_loop(),
        mcp.run_sse(host, port),
    )
```

`poll_loop` is a new async function (not the existing synchronous `run_loop`):

```python
async def poll_loop(service, repo, interval, lock, poll_state):
    while True:
        async with lock:
            result = await asyncio.to_thread(service.run_once, repo)
        poll_state["last_poll_time"] = datetime.utcnow().isoformat()
        poll_state["last_poll_result"] = result
        await asyncio.sleep(interval)
```

The existing synchronous `run_loop` in `main2main_orchestrator.py` remains unchanged for CLI usage.

## Intermediate State During Terminal Analysis

When reconcile determines a PR needs terminal analysis, it must prevent duplicate processing on subsequent poll cycles. The flow:

1. `reconcile` / `apply_fixup_outcome` sets `status = "pending_terminal"` (new status value) and enqueues a `TerminalJob`
2. `run_once` skips PRs with `status = "pending_terminal"` (same as it currently skips `"manual_review"`)
3. Terminal worker completes → sets `status = "manual_review"`

This avoids both blocking and duplicate job creation.

## Graceful Shutdown

On SIGTERM (sent by `systemctl stop`):

1. The asyncio event loop cancels all tasks via standard `asyncio.CancelledError` propagation
2. The reconcile poller abandons its current `asyncio.sleep` and exits. If a `run_once` call is in progress in the thread pool, it runs to completion. Note: `run_once` can take up to ~30 seconds in the worst case (e.g., `_wait_for_dispatched_fixup_run` polls up to 10 times with 2-second intervals). The `TimeoutStopSec=60` in the systemd unit accommodates this
3. The terminal worker checks for cancellation between job steps. If cancelled mid-job, the job remains `"pending"` in the state file and will be resumed on next startup
4. The MCP server stops accepting new connections and closes existing ones

The `Restart=on-failure` systemd directive only triggers on non-zero exit codes, not on clean SIGTERM shutdown.

## Deployment

### systemd unit

```ini
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

### Environment variables

```
GITHUB_TOKEN=...
ANTHROPIC_BASE_URL=...
ANTHROPIC_AUTH_TOKEN=...
STATE_PATH=/var/lib/vllm-benchmarks-orchestrator/state.json
POLL_INTERVAL=60
REPO=nv-action/vllm-benchmarks
MCP_HOST=127.0.0.1
MCP_PORT=8080
```

### Hardware requirements

- Minimum: 2 vCPU, 2 GB RAM, 10 GB disk
- Stable outbound access to GitHub API and Claude gateway
- No GPU/NPU required

## Terminal Analysis Async Flow

Current (blocking):
```
reconcile → _build_manual_review_issue → extract_e2e_failure_analysis (subprocess, slow)
                                       → summarize_manual_review_issue (HTTP to Claude, slow)
                                       → create_manual_review_issue
          → set status = "manual_review"
```

New (async):
```
reconcile → set status = "pending_terminal"
          → enqueue TerminalJob
          → return immediately
                                    ↓
terminal worker (async task) → to_thread(extract_e2e_failure_analysis)  [skipped for phase3_no_changes]
                             → to_thread(summarize_manual_review_issue)  [done_failure only]
                             → build_phase3_no_changes_issue(...)       [phase3_no_changes only]
                             → create_manual_review_issue
                             → acquire service_lock
                             → set status = "manual_review"
                             → remove job from queue
```

Reconcile loop is never blocked. Terminal worker processes jobs one at a time in the background.

## Job Persistence and Restart Safety

Terminal jobs are persisted to the state file under a `_terminal_jobs` key:

```json
{
  "nv-action/vllm-benchmarks#154": { "...state..." },
  "_terminal_jobs": [
    {
      "pr_number": 154,
      "repo": "nv-action/vllm-benchmarks",
      "terminal_reason": "done_failure",
      "e2e_run_id": "12345",
      "e2e_run_url": "https://...",
      "fixup_run_id": null,
      "status": "pending"
    }
  ]
}
```

On startup:
1. Load `_terminal_jobs` from state file
2. Resume any jobs with `status: "pending"`
3. Mark completed jobs as `"done"` and remove from the list

**Idempotency and crash safety:**

The dangerous crash window is: issue created on GitHub, but job not yet marked done in state. On restart, the worker would create a duplicate issue.

To prevent this:
1. Every manual-review issue body includes a machine-readable marker comment, for example: `<!-- main2main-manual-review repo=nv-action/vllm-benchmarks pr=154 phase=done fixup_run_id=12345 -->`.
2. Before creating an issue, the worker queries GitHub for an existing issue containing that exact marker.
3. If a matching issue exists, the worker skips creation and marks the job done.
4. After successfully creating an issue, the worker writes the issue URL into the job record and marks it done in a single state-file write. The marker is the primary dedupe key; the URL is the persisted confirmation.

## Validation Requirements

1. Existing `tests/main2main/test_orchestrator.py` continues to pass unchanged
2. MCP tools return correct results against a mock `OrchestratorService`
3. MCP tools return proper error responses on failures (not crashes)
4. Terminal worker processes jobs without blocking the reconcile poller
5. Terminal worker correctly handles both `done_failure` and `phase3_no_changes` reasons
6. Terminal jobs survive process restart and are resumed exactly once
7. Repeated `run_once` calls remain idempotent
8. PRs in `pending_terminal` status are skipped by `run_once`
9. CLI in `main2main_orchestrator.py` remains fully functional after extraction
10. Graceful shutdown on SIGTERM does not lose pending terminal jobs
11. Service lock prevents concurrent control operations from overlapping
12. Terminal worker deduplicates issue creation on restart (idempotency key check)
13. MCP server binds to localhost by default

## Migration Path

1. Extract `github_adapter.py`, `state_store.py` — update imports in `main2main_orchestrator.py`
2. Add `terminal_worker.py` — modify `OrchestratorService` to enqueue instead of blocking, add `pending_terminal` status
3. Add `mcp_server.py`
4. Add `service_main.py` + systemd assets
5. Deploy, validate with staging PR
6. Existing CLI continues to work throughout — no breaking changes
