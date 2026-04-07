# Main2Main Workflow-Native Orchestration Design

> Replaces the local `main2main` orchestrator/service with GitHub Actions workflows and PR comment state, while preserving the post-`5c867bde` automation semantics.

## Context

The current `main2main` system is split across:

- [`main2main_auto.yaml`](../../../.github/workflows/main2main_auto.yaml) for detect-and-adapt and explicit fixup execution
- [`main2main_manual_review.yaml`](../../../.github/workflows/main2main_manual_review.yaml) for terminal issue creation
- [`bisect_vllm.yaml`](../../../.github/workflows/bisect_vllm.yaml) for long-running bisect
- `main2main_orchestrator.py`, `github_adapter.py`, `service_main.py`, `mcp_server.py`, `terminal_worker.py`, and `state_store.py` for local polling, state persistence, and terminal actions

Today the production control plane is local:

1. Local service polls open `main2main` PRs
2. Local state decides `mark_ready`, `dispatch_fixup`, or `manual_review`
3. Local `gh` performs PR-ready and workflow dispatch actions

The target state is a workflow-native control plane:

1. GitHub Actions own all polling/state transitions
2. PR comments replace local state files
3. No production path depends on a local daemon or local write-capable `gh`

## Goal

Move all `main2main` orchestration into workflows while preserving existing flow semantics:

1. Detect upstream vLLM commit drift and open at most one active `main2main` PR
2. Track workflow state on the PR itself using a fixed structured comment
3. Wait for E2E entirely in workflows
4. Run Phase 2 and Phase 3 fixes entirely in workflows
5. Support long-running bisect without forcing a single giant Phase 3 run
6. Keep `bisect_vllm.yaml` usable as a standalone bisect workflow
7. Remove the local orchestrator/service stack from production use

## Non-Goals

- Automatic conflict resolution or auto-rebase
- Changing the PR-triggered E2E mechanism in [`pr_test_full.yaml`](../../../.github/workflows/pr_test_full.yaml)
- Replacing Claude-based fix generation or manual-review issue generation
- Generalizing this system to other repositories
- Preserving the local MCP service as a supported control plane

## Design Approaches Considered

### Approach A: Single giant `main2main_auto.yaml`

One workflow handles detect, waiting, phase2, phase3, terminal actions, and bisect callback logic.

Rejected: state transitions and long-running waits become too hard to reason about; terminal side effects and reconcile logic blur together.

### Approach B: Workflow-native control plane with focused workflows

Use:

- `main2main_auto.yaml`
- `main2main_reconcile.yaml`
- `main2main_terminal.yaml`
- `bisect_vllm.yaml`

Selected: closest to current responsibilities, easiest to test, and safest for one-shot migration.

### Approach C: Keep local reconcile, move only fix/terminal into workflows

Rejected: still requires local write-capable `gh`, local service management, and split-brain debugging.

## Architecture

The system is split into four workflows and one main orchestration helper script.

### Workflows

| Workflow | Responsibility |
|---|---|
| `main2main_auto.yaml` | `detect`, `fix_phase2`, `fix_phase3_prepare`, `fix_phase3_finalize` |
| `main2main_reconcile.yaml` | Scheduled/manual reconcile for `waiting_e2e`, plus `waiting_bisect` recovery |
| `main2main_terminal.yaml` | Terminal actions: `make_ready` and `manual_review` |
| `bisect_vllm.yaml` | Bisect executor; optionally callbacks into main2main finalize |

### Shared orchestration scripts

| Script | Responsibility |
|---|---|
| `.github/workflows/scripts/main2main_ci.py` | PR state comment I/O, stale guards, PR context, mergeability checks, reconcile decisions, fix-result parsing |
| [`bisect_helper.py`](../../../.github/workflows/scripts/bisect_helper.py) | Existing bisect matrix/env resolution plus new result-json and callback-payload helpers |
| [`ci_log_summary.py`](../../../.github/workflows/scripts/ci_log_summary.py) | Existing CI log extraction for `bisect-json` and manual-review analysis |
| [`bisect_vllm.sh`](../../../.github/workflows/scripts/bisect_vllm.sh) | Existing bisect execution logic |

This keeps orchestration logic concentrated in one new script, rather than scattering state machine code across many YAML steps or many tiny Python files.

## State Model

### Comments

Each PR uses two structured comments:

1. `main2main-register`
   - bootstrap metadata
   - created during detect
   - updated when `head_sha` or `phase` changes

2. `main2main-state`
   - single live source of truth
   - updated in-place by workflows
   - replaces local `state.json`

### `main2main-state` schema

```json
{
  "pr_number": 188,
  "branch": "main2main_auto_2026-04-03_08-22",
  "head_sha": "1ac49ff7b834177ba43fb7a3044269908bdcbef5",
  "old_commit": "35141a7eeda941a60ad5a4956670c60fd5a77029",
  "new_commit": "fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
  "phase": "3",
  "status": "waiting_bisect",
  "e2e_run_id": "24000000000",
  "fix_run_id": "24000000001",
  "bisect_run_id": "24000000002",
  "dispatch_token": "m2m-188-phase3-20260407T120000Z",
  "terminal_reason": "",
  "manual_review_issue_url": "",
  "last_transition": "fix_phase3_prepare->waiting_bisect",
  "updated_at": "2026-04-07T12:00:00Z",
  "updated_by": "main2main_auto.yaml/fix_phase3_prepare"
}
```

### State fields

| Field | Purpose |
|---|---|
| `pr_number`, `branch`, `head_sha` | PR execution context |
| `old_commit`, `new_commit` | Current vLLM commit range |
| `phase` | Existing main2main phase semantics: `2`, `3`, `done` |
| `status` | Live run state |
| `e2e_run_id` | Current E2E run being consumed |
| `fix_run_id` | Current fix workflow run |
| `bisect_run_id` | Current bisect workflow run |
| `dispatch_token` | Current action-attempt token; guards against stale fix/bisect/terminal callbacks |
| `terminal_reason` | Reason for `manual_review` |
| `manual_review_issue_url` | Created issue link |
| `last_transition`, `updated_at`, `updated_by` | Audit/debugging |

### Allowed statuses

- `waiting_e2e`
- `fixing`
- `waiting_bisect`
- `ready`
- `manual_review`
- `error`

`waiting_bisect` is a first-class persistent state. It is required because bisect can run for hours and must not be coupled to the same run that later performs Claude-based Phase 3 repair.

### Token rotation rule

`dispatch_token` is not minted once at detect time and then reused forever.

A fresh `dispatch_token` must be generated and persisted in `main2main-state` before every outbound action attempt:

- reconcile -> `fix_phase2`
- reconcile -> `fix_phase3_prepare`
- reconcile -> terminal `make_ready`
- reconcile -> terminal `manual_review`
- `fix_phase3_prepare` -> `bisect_vllm.yaml`
- `bisect_vllm.yaml` callback -> `fix_phase3_finalize`

Every callback or terminal workflow must match the exact current token in state before mutating state or performing side effects.

## Workflow Contracts

### `main2main_auto.yaml`

Modes:

| Mode | Inputs |
|---|---|
| `detect` | `target_commit` optional |
| `fix_phase2` | `pr_number`, `dispatch_token` |
| `fix_phase3_prepare` | `pr_number`, `dispatch_token` |
| `fix_phase3_finalize` | `pr_number`, `dispatch_token`, `bisect_run_id` |

Responsibilities:

- Detect vLLM drift and create/update the PR bootstrap comments
- Run Claude-driven Phase 2 repair
- Prepare Phase 3 bisect dispatch
- Finalize Phase 3 using bisect outputs and Claude repair

Fix modes do not receive `e2e_run_id` as a dispatch input. They read the persisted `e2e_run_id` from `main2main-state`.

### `main2main_reconcile.yaml`

Triggers:

- `schedule`
- `workflow_dispatch`

Inputs:

| Input | Purpose |
|---|---|
| `pr_number` optional | Manually reconcile one PR |

Responsibilities:

- List open `main2main` PRs
- Initialize missing `main2main-state` comments from `main2main-register`
- Recover missing comments for a newly created detect PR by parsing PR body/labels when the detect run created the PR but failed before writing structured comments
- Resolve E2E runs for `waiting_e2e`
- Dispatch next action
- Recover `waiting_bisect` if bisect finished but finalize did not run

### `main2main_terminal.yaml`

Inputs:

| Action | Inputs |
|---|---|
| `make_ready` | `pr_number`, `dispatch_token` |
| `manual_review` | `pr_number`, `dispatch_token`, `terminal_reason` |

Responsibilities:

- `gh pr ready`
- Generate manual-review issue body using existing Claude + CI-analysis flow
- Create issue
- Patch `main2main-state` to `ready` or `manual_review`

Terminal actions also read `e2e_run_id`, `fix_run_id`, and other context from `main2main-state` rather than from long workflow-dispatch inputs.

### `bisect_vllm.yaml`

Retains standalone use. New optional inputs:

| Input | Default | Purpose |
|---|---|---|
| `caller_type` | `standalone` | `standalone` or `main2main` |
| `main2main_pr_number` | `''` | Callback context for main2main |
| `main2main_dispatch_token` | `''` | Stale guard for callback |

Callback rule:

- Only if `caller_type == main2main`
- Mint and persist a fresh `dispatch_token` in `main2main-state`
- Dispatch `main2main_auto.yaml mode=fix_phase3_finalize`
- Pass only `pr_number`, `dispatch_token`, `bisect_run_id`

This avoids polluting standalone bisect use with main2main-only behavior.

## Detailed Flow

### 1. Detect path

`main2main_auto.yaml mode=detect`

1. Resolve pinned `old_commit` and target `new_commit`
2. If unchanged: exit
3. If an open `main2main` PR already exists: exit
4. Claude adapts the branch with `main2main` skill
5. Create draft PR with labels `main2main`, `ready`, `ready-for-test`
6. Write `main2main-register`
7. Write `main2main-state` with:
   - `phase=2`
   - `status=waiting_e2e`
   - `head_sha=<current PR head>`
   - fresh `dispatch_token`
8. Exit

Detect must treat PR creation plus comment creation as one failure-checked transaction. If the PR is created but one or both structured comments are missing, reconcile must be able to recover the state from PR metadata/body on a later pass instead of leaving an unrecoverable orphan PR.

E2E is not waited on here. It is triggered by PR labels and future PR synchronize events via [`pr_test_full.yaml`](../../../.github/workflows/pr_test_full.yaml).

### 2. Reconcile path

`main2main_reconcile.yaml`

For each open `main2main` PR:

1. Load/initialize `main2main-state`
2. If mergeability is explicitly conflicting (`mergeable=CONFLICTING` or an equivalent dirty/conflict status):
   - persist the current failed/terminal context, including `e2e_run_id` when available
   - mint and persist a fresh `dispatch_token`
   - dispatch terminal manual review with `terminal_reason=merge_conflict`
   - stop automatic processing for that PR
3. If `status=waiting_e2e`:
   - resolve latest E2E run for the exact `head_sha`
   - ignore E2E runs for older heads; reconcile must never advance state from a stale PR commit
   - if run absent/in-progress: keep `waiting_e2e`
   - if success: persist `e2e_run_id`, mint a fresh `dispatch_token`, then dispatch terminal `make_ready`
   - if failure and `phase=2`: persist `e2e_run_id`, patch to `phase=2,status=fixing`, mint a fresh `dispatch_token`, then dispatch `fix_phase2`
   - if failure and `phase=3`: persist `e2e_run_id`, patch to `phase=3,status=fixing`, mint a fresh `dispatch_token`, then dispatch `fix_phase3_prepare`
   - if failure and `phase=done`: persist `e2e_run_id`, mint a fresh `dispatch_token`, then dispatch terminal manual review with `terminal_reason=done_failure`
4. If `status=waiting_bisect`:
   - check `bisect_run_id`
   - if bisect finished but finalize callback is missing/failed: mint a fresh `dispatch_token`, persist it, then re-dispatch `fix_phase3_finalize`
5. Skip `fixing`, `ready`, `manual_review`, and `error` unless explicitly targeted by recovery logic

### 3. Phase 2 fix path

`main2main_auto.yaml mode=fix_phase2`

1. Validate stale guard: `phase=2,status=fixing`, matching `dispatch_token`
2. Record `fix_run_id=${github.run_id}`
3. Run Claude with `main2main-error-analysis` skill
4. Parse result
5. If changes were pushed:
   - commit and push
   - patch register comment with new `head_sha`
   - patch state to `phase=3,status=waiting_e2e`
   - in this branch, `waiting_e2e` means waiting for a brand new E2E run for the new `head_sha`
6. If no changes:
   - preserve the existing behavior by patching state to `phase=3,status=waiting_e2e`
   - keep the same `head_sha`
   - allow the next reconcile pass to consume the already-failed E2E under Phase 3 semantics
   - in this branch, `waiting_e2e` is a compatibility state name; it does not imply that a new E2E run is expected, because the `head_sha` did not change
7. If workflow fails:
   - mint and persist a fresh `dispatch_token`
   - dispatch terminal manual review with `terminal_reason=fixup_failure`

This preserves the current semantic that after Phase 2 execution, the next failed E2E should route into Phase 3.

### `bisect_vllm.yaml` calling modes

| Calling mode | Inputs | Callback behavior |
|---|---|---|
| Standalone | default `caller_type=standalone` | runs bisect only; never dispatches `fix_phase3_finalize` |
| Main2Main | `caller_type=main2main`, `main2main_pr_number`, `main2main_dispatch_token` | runs bisect and, on completion, dispatches `fix_phase3_finalize` with the current guarded token |

For `caller_type=main2main`, bisect runs use per-PR concurrency `main2main-bisect-pr-${main2main_pr_number}` from the `Concurrency` section.

### 4. Phase 3 prepare path

`main2main_auto.yaml mode=fix_phase3_prepare`

1. Validate stale guard: `phase=3,status=fixing`
2. Record `fix_run_id=${github.run_id}`
3. Run `ci_log_summary.py --format bisect-json`
4. Extract representative `test_cmd`
5. If no `test_cmd`, use the existing stable fallback commands
6. Mint and persist a fresh `dispatch_token`
7. Dispatch `bisect_vllm.yaml` with:
   - `caller_type=main2main`
   - `main2main_pr_number`
   - `main2main_dispatch_token`
   - existing `good_commit`, `bad_commit`, `test_cmd`, `caller_run_id`
8. Resolve the bisect run id
9. Patch state to:
   - `phase=3`
   - `status=waiting_bisect`
   - `bisect_run_id=<run>`
10. Exit

### 5. Phase 3 finalize path

`main2main_auto.yaml mode=fix_phase3_finalize`

1. Validate stale guard:
   - `phase=3,status=waiting_bisect`
   - matching `dispatch_token`
   - matching `bisect_run_id`
2. Record `fix_run_id=${github.run_id}`
3. Download `bisect-summary` and `bisect_result.json`
4. Run Claude with bisect-guided repair using the top-level summary plus full `group_results`
5. If changes were pushed:
   - commit and push
   - patch register comment with new `head_sha`
   - patch state to `phase=done,status=waiting_e2e`
6. If no changes:
   - dispatch terminal manual review with `terminal_reason=phase3_no_changes`
7. If bisect artifacts are missing:
   - still allow best-effort Claude repair
   - only escalate to manual review if finalize still produces no useful result
8. If finalize workflow itself fails:
   - mint and persist a fresh `dispatch_token`
   - dispatch terminal manual review with `terminal_reason=workflow_error`

### 6. Terminal path

`main2main_terminal.yaml`

#### `action=make_ready`

1. Validate guard: PR still matches state `head_sha`
2. Validate PR is not conflicting
3. Check whether the PR is already non-draft; if so, treat that as converged success
4. Otherwise run `gh pr ready`
5. Patch state to:
   - `phase=done`
   - `status=ready`

#### `action=manual_review`

1. Validate state is not already terminal
2. Check whether a manual-review issue for the current terminal attempt already exists; if so, treat that as converged success
3. Otherwise generate issue content using current manual-review analysis flow
4. Create issue
5. Patch state to:
   - `phase=done`
   - `status=manual_review`
   - `terminal_reason=<reason>`
   - `manual_review_issue_url=<issue>`

## Conflict Handling

Automatic conflict resolution is out of scope.

Two conflict classes are handled explicitly:

1. **PR merge conflict**
   - detected only through explicit PR conflict/dirty mergeability states
   - automatic flow stops
   - terminal manual review with `terminal_reason=merge_conflict`

2. **Push/rebase conflict**
   - e.g. `git push --force-with-lease` failure
   - treated as workflow failure
   - routed to manual review via `terminal_reason=workflow_error`

This keeps the state machine simple and avoids dangerous automatic rebases on top of Claude-generated repair commits.

## Stale Guard Rules

Every workflow that can mutate state or perform terminal side effects must validate:

| Guard | Why |
|---|---|
| `pr_number` matches target PR | avoid cross-PR contamination |
| current PR `head_sha` matches state | avoid acting on stale commit context |
| expected `phase` matches state | avoid phase confusion |
| expected `status` matches state | avoid invalid transitions |
| `dispatch_token` matches state | avoid stale fix/bisect callbacks |

If a guard fails, the workflow exits cleanly without writing state.

## Concurrency

Use GitHub Actions concurrency to serialize per-PR state mutation:

| Workflow | Concurrency group | cancel-in-progress |
|---|---|---|
| `main2main_auto.yaml` detect | `main2main-detect` | `false` |
| `main2main_reconcile.yaml` | `main2main-reconcile` | `false` |
| `main2main_auto.yaml` fix modes | `main2main-pr-${pr_number}` | `false` |
| `main2main_terminal.yaml` | `main2main-pr-${pr_number}` | `false` |
| `bisect_vllm.yaml` main2main calls | `main2main-bisect-pr-${main2main_pr_number}` | `false` |

`cancel-in-progress` remains `false` because long bisect runs should be allowed to complete; stale runs will self-suppress via guards rather than hard cancellation.

## Failure Handling

### Retriable waits

These keep the current state and rely on later reconcile runs:

- E2E run not found yet
- E2E still running
- Bisect still running
- Bisect finished but finalize callback did not happen

### Best-effort continuation

These do not immediately go terminal:

- `bisect-json` lacks a representative `test_cmd`
- bisect summary artifact missing
- bisect run failed but partial context exists

In these cases Phase 3 finalize still attempts Claude repair before giving up.

### Terminal failures

These transition to manual review:

| Scenario | `terminal_reason` |
|---|---|
| `phase=done` E2E failure | `done_failure` |
| Phase 2 workflow failure | `fixup_failure` |
| Phase 3 finalize produced no changes | `phase3_no_changes` |
| merge conflict | `merge_conflict` |
| bisect/finalize irrecoverable workflow failure | `workflow_error` or `bisect_failed` |

### `error` status

Reserved for protocol/storage failures such as:

- malformed state comment
- state comment update failure
- unrecoverable metadata corruption

Unlike `manual_review`, `error` indicates system failure rather than business-terminal failure.

## Bisect Result Format

`bisect_vllm.yaml` must publish a machine-readable `bisect_result.json`, derived from current helper/script outputs.

Minimum fields:

```json
{
  "caller_type": "main2main",
  "caller_run_id": "24000000000",
  "bisect_run_id": "24000000002",
  "status": "success",
  "good_commit": "35141a7eeda941a60ad5a4956670c60fd5a77029",
  "bad_commit": "fa9e68022d29c5396dfbb96d13587b6bc1bdb933",
  "test_cmd": "pytest -sv tests/e2e/...",
  "first_bad_commit": "abc123...",
  "first_bad_commit_url": "https://github.com/vllm-project/vllm/commit/abc123...",
  "total_steps": 7,
  "total_commits": 20,
  "skipped_commits": ["..."],
  "log_entries": ["..."],
  "group_results": []
}
```

This supplements, not replaces, the existing markdown summary artifact.

Aggregation semantics:

- If all successful groups report the same `first_bad_commit`, top-level `status=success` and `first_bad_commit` is that value.
- If successful groups disagree on `first_bad_commit`, top-level `status=ambiguous`, top-level `first_bad_commit` is empty, and finalize must inspect `group_results`.
- If some groups succeed and some fail, top-level `status=partial_success`; finalize still runs with both top-level summary and `group_results`.
- If no group yields a usable culprit, top-level `status=failed`.

## File Changes

### Create

- `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md`
- `.github/workflows/main2main_reconcile.yaml`
- `.github/workflows/main2main_terminal.yaml`
- `.github/workflows/scripts/main2main_ci.py`

### Modify

- [`main2main_auto.yaml`](../../../.github/workflows/main2main_auto.yaml)
- [`bisect_vllm.yaml`](../../../.github/workflows/bisect_vllm.yaml)
- [`bisect_helper.py`](../../../.github/workflows/scripts/bisect_helper.py)
- `deploy/systemd/vllm-benchmarks-orchestrator.service`
- `deploy/systemd/orchestrator.env.example`
- existing tests under `tests/main2main/`
- `tests/main2main/test_deploy_assets.py`

### Delete

- `main2main_orchestrator.py`
- `github_adapter.py`
- `service_main.py`
- `mcp_server.py`
- `terminal_worker.py`
- `state_store.py`
- `tests/main2main/test_service_main.py`
- `tests/main2main/test_github_adapter.py`
- legacy tests that only cover the removed local control plane

### Rename

- [`main2main_manual_review.yaml`](../../../.github/workflows/main2main_manual_review.yaml) -> `main2main_terminal.yaml`

## Testing Strategy

### Unit tests

- state comment parse/update
- stale guard validation
- reconcile decision logic
- fix-result parsing
- bisect result aggregation

### Workflow contract tests

- `main2main_auto.yaml` modes and required inputs
- `main2main_reconcile.yaml` state-handling contract
- `main2main_terminal.yaml` action contract
- `bisect_vllm.yaml` callback only when `caller_type=main2main`
- `make_ready` idempotency when the PR is already non-draft
- `manual_review` idempotency when an issue already exists but state was not yet patched

### Integration tests

- detect -> `waiting_e2e`
- phase2 failure -> `fix_phase2` -> `phase=3,status=waiting_e2e`
- phase3 prepare -> `waiting_bisect` -> finalize -> `waiting_e2e` or `manual_review`
- done failure -> `manual_review`
- success -> `ready`
- detect-created PR with missing comments -> reconcile recovery
- existing manual-review issue with stale non-terminal state -> terminal run converges without creating a duplicate issue

### GitHub smoke tests

1. Detect opens a PR and writes both comments
2. E2E success routes to `make_ready`
3. E2E failure routes to Phase 2
4. Phase 3 routes through bisect and finalize
5. Standalone `bisect_vllm.yaml` does not callback finalize

## Cutover Plan

This is a one-shot cutover, but execution still happens in a controlled order:

1. Add new workflow-native scripts and tests
2. Implement workflow-native state comment protocol
3. Convert workflows to new contracts
4. Validate full `tests/main2main`
5. Run GitHub smoke tests
6. Remove local orchestrator/service production paths
7. Re-run full regression to ensure no remaining production references

The final repository state must have no production path that requires:

- local `service_main.py`
- local JSON state
- local `gh` write operations
- local MCP service

## Success Criteria

The migration is complete when all of the following are true:

1. A new main2main PR can be created without any local service running
2. E2E success transitions the PR to `ready`
3. Phase 2 and Phase 3 run entirely via workflows
4. Bisect can run for main2main and standalone use without behavior conflicts
5. Manual review issues are created entirely by workflows
6. All live state is visible in PR comments
7. Local orchestrator/service files are removed from the production path
8. A PR that becomes already-ready before state patch can still converge to `ready`
