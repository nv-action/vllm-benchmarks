# Main2Main Workflow-Native Orchestration Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the local `main2main` orchestrator/service with workflow-native orchestration driven by PR comments, while preserving current post-`5c867bde` behavior and keeping `bisect_vllm.yaml` usable as a standalone workflow.

**Architecture:** Move state and transition logic into a new `.github/workflows/scripts/main2main_ci.py` helper plus four workflows: `main2main_auto.yaml`, `main2main_reconcile.yaml`, `main2main_terminal.yaml`, and `bisect_vllm.yaml`. Preserve the existing phase semantics (`2`, `3`, `done`) and store live state in a single mutable `main2main-state` PR comment. Route all workflow dispatches through short input contracts keyed by `pr_number`, `dispatch_token`, and persisted state rather than long `workflow_dispatch` payloads.

**Tech Stack:** GitHub Actions YAML, Python 3 CLI helpers, `gh`, `pytest`, shell (`bash`), existing `ci_log_summary.py`, `bisect_helper.py`, and `bisect_vllm.sh`.

---

## File Structure

### Create

- `.github/workflows/main2main_reconcile.yaml`
- `.github/workflows/main2main_terminal.yaml`
- `.github/workflows/scripts/main2main_ci.py`
- `tests/main2main/test_main2main_ci.py`

### Modify

- `.github/workflows/main2main_auto.yaml`
- `.github/workflows/bisect_vllm.yaml`
- `.github/workflows/scripts/bisect_helper.py`
- `tests/main2main/test_main2main_workflow_contract.py`
- `tests/main2main/test_bisect_helper_runtime_env.py`
- `tests/main2main/test_extract_and_analyze_contract.py`
- `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md`

### Delete or Rename

- Rename `.github/workflows/main2main_manual_review.yaml` -> `.github/workflows/main2main_terminal.yaml`
- Delete `main2main_orchestrator.py`
- Delete `github_adapter.py`
- Delete `service_main.py`
- Delete `mcp_server.py`
- Delete `terminal_worker.py`
- Delete `state_store.py`
- Delete `deploy/systemd/vllm-benchmarks-orchestrator.service`
- Delete `deploy/systemd/orchestrator.env.example`
- Delete `tests/main2main/test_orchestrator.py`
- Delete `tests/main2main/test_github_adapter.py`
- Delete `tests/main2main/test_service_main.py`
- Delete `tests/main2main/test_mcp_server.py`
- Delete `tests/main2main/test_state_store.py`
- Delete `tests/main2main/test_deploy_assets.py`
- Delete `tests/main2main/test_fixup_dispatch_contract.py`

## Chunk 0: Workspace Safety

### Task 0: Create an isolated worktree before touching implementation files

**Files:**
- Create: a new git worktree outside the main checkout

- [ ] **Step 1: Create a dedicated implementation worktree**

Use `@using-git-worktrees` before editing code. One acceptable command sequence is:

```bash
git -C /Users/antarctica/Work/PR/vllm-benchmarks worktree add \
  /Users/antarctica/Work/PR/vllm-benchmarks-main2main-workflow-native \
  -b main2main-workflow-native
```

Expected: a clean new worktree is created on branch `main2main-workflow-native`.

- [ ] **Step 2: Verify the new worktree is isolated and clean**

Run:

```bash
git -C /Users/antarctica/Work/PR/vllm-benchmarks-main2main-workflow-native status --short
```

Expected: no output

- [ ] **Step 3: Use the new worktree for every remaining task in this plan**

All following `git add`, `git commit`, `pytest`, and file edits should run from:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks-main2main-workflow-native
```

## Chunk 1: Shared State/Decision Helpers

### Task 1: Add workflow-native state helper and port state-machine coverage

**Files:**
- Create: `.github/workflows/scripts/main2main_ci.py`
- Create: `tests/main2main/test_main2main_ci.py`
- Modify: `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md`

- [ ] **Step 1: Write failing tests for state comment parsing and stale guards**

Create `tests/main2main/test_main2main_ci.py` with coverage for:

```python
def test_parse_main2main_state_comment_round_trip():
    ...

def test_guard_check_rejects_mismatched_dispatch_token():
    ...

def test_phase2_no_changes_moves_to_phase3_waiting_e2e_without_new_head():
    ...

def test_reconcile_ignores_e2e_runs_for_old_heads():
    ...
```

- [ ] **Step 2: Run the new helper tests and confirm they fail**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_ci.py
```

Expected: FAIL because `main2main_ci.py` does not exist and the workflow-native helper APIs are not implemented.

- [ ] **Step 3: Implement `main2main_ci.py` with subcommands and pure helpers**

Implement one focused script with subcommands/functions for:

- comment discovery and update for `main2main-register` and `main2main-state`
- `dispatch_token` minting
- stale guard validation
- PR mergeability check
- exact-`head_sha` E2E matching
- reconcile action decision
- fix-result parsing helpers

Keep the file CLI-friendly so workflows can call:

```bash
python3 .github/workflows/scripts/main2main_ci.py state-read ...
python3 .github/workflows/scripts/main2main_ci.py state-write ...
python3 .github/workflows/scripts/main2main_ci.py reconcile-decision ...
```

- [ ] **Step 4: Run helper tests until they pass**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_ci.py
```

Expected: PASS

- [ ] **Step 5: Port the business-semantic coverage from the deleted orchestrator tests**

Move the important assertions from `tests/main2main/test_orchestrator.py` into `tests/main2main/test_main2main_ci.py`:

- `phase=2` changes -> `phase=3,status=waiting_e2e`
- `phase=2` no changes -> `phase=3,status=waiting_e2e`
- `phase=3` changes -> `phase=done,status=waiting_e2e`
- `phase=3` no changes -> terminal/manual review
- `done` failure -> terminal/manual review

- [ ] **Step 6: Re-run the helper tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_ci.py
```

Expected: PASS

- [ ] **Step 7: Commit Chunk 1 helper work**

```bash
git add .github/workflows/scripts/main2main_ci.py tests/main2main/test_main2main_ci.py docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md
git commit -m "feat: add workflow-native main2main state helpers"
```

### Task 2: Extend bisect helper output for callback-safe machine-readable results

**Files:**
- Modify: `.github/workflows/scripts/bisect_helper.py`
- Modify: `tests/main2main/test_bisect_helper_runtime_env.py`
- Modify: `tests/main2main/test_extract_and_analyze_contract.py`

- [ ] **Step 1: Add failing tests for `bisect_result.json` aggregation**

Add tests covering:

```python
def test_bisect_result_success_when_all_groups_agree():
    ...

def test_bisect_result_ambiguous_when_groups_disagree():
    ...

def test_bisect_result_partial_success_when_some_groups_fail():
    ...

def test_bisect_result_standalone_callback_payload_is_empty():
    ...
```

- [ ] **Step 2: Run the bisect helper tests and confirm they fail**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_bisect_helper_runtime_env.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: FAIL on the new aggregation/callback tests.

- [ ] **Step 3: Implement result-json and callback-payload helpers in `bisect_helper.py`**

Add focused helpers for:

- building top-level `bisect_result.json`
- aggregating matrix group results into `success`, `ambiguous`, `partial_success`, or `failed`
- emitting callback metadata only when `caller_type=main2main`

- [ ] **Step 4: Re-run the bisect helper tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_bisect_helper_runtime_env.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

- [ ] **Step 5: Commit Chunk 1 bisect-helper work**

```bash
git add .github/workflows/scripts/bisect_helper.py tests/main2main/test_bisect_helper_runtime_env.py tests/main2main/test_extract_and_analyze_contract.py
git commit -m "feat: add bisect result aggregation helpers"
```

## Chunk 2: Workflow Migration

### Task 3: Rename and rewrite terminal workflow for `make_ready` and `manual_review`

**Files:**
- Rename: `.github/workflows/main2main_manual_review.yaml` -> `.github/workflows/main2main_terminal.yaml`
- Modify: `tests/main2main/test_main2main_workflow_contract.py`

- [ ] **Step 1: Add failing contract tests for the new terminal workflow**

Extend `tests/main2main/test_main2main_workflow_contract.py` with checks for:

- workflow file is named `main2main_terminal.yaml`
- `action=make_ready` exists
- `action=manual_review` exists
- `make_ready` is idempotent when the PR is already non-draft
- `manual_review` is idempotent when an issue already exists

- [ ] **Step 2: Run the workflow contract tests and confirm failures**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_workflow_contract.py
```

Expected: FAIL because the workflow file and inputs still match the old manual-review-only contract.

- [ ] **Step 3: Rename the workflow and implement the terminal actions**

Use `git mv` for the file rename, then implement:

- `workflow_dispatch` inputs: `action`, `pr_number`, `dispatch_token`, `terminal_reason`
- `make_ready`: read state, confirm draft status, call `gh pr ready` only if needed, patch state to `ready`
- `manual_review`: read state, detect existing issue if present, otherwise run current issue-generation flow and patch state to `manual_review`

- [ ] **Step 4: Re-run the workflow contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_workflow_contract.py
```

Expected: PASS for the terminal workflow contract assertions.

- [ ] **Step 5: Commit the terminal workflow migration**

```bash
git add .github/workflows/main2main_terminal.yaml tests/main2main/test_main2main_workflow_contract.py
git commit -m "feat: add workflow-native main2main terminal actions"
```

### Task 4: Add `main2main_reconcile.yaml` and wire exact-head E2E progression

**Files:**
- Create: `.github/workflows/main2main_reconcile.yaml`
- Modify: `.github/workflows/scripts/main2main_ci.py`
- Modify: `tests/main2main/test_main2main_workflow_contract.py`

- [ ] **Step 1: Add failing contract tests for reconcile**

Add tests that assert:

- the workflow exists
- it supports `schedule` and `workflow_dispatch`
- it handles `waiting_e2e`
- it includes `waiting_bisect` recovery
- it dispatches `make_ready`, `fix_phase2`, `fix_phase3_prepare`, and terminal manual review based on persisted state

- [ ] **Step 2: Run the contract tests and confirm failures**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_workflow_contract.py
```

Expected: FAIL because `main2main_reconcile.yaml` does not exist yet.

- [ ] **Step 3: Implement `main2main_reconcile.yaml`**

Use short jobs and `main2main_ci.py` helpers to:

- list open `main2main` PRs
- bootstrap missing state from comments/PR metadata
- resolve the E2E run for the exact `head_sha`
- mint and persist fresh `dispatch_token`s before dispatch
- recover `waiting_bisect` when the bisect callback did not fire

- [ ] **Step 4: Re-run the contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main/test_main2main_workflow_contract.py
```

Expected: PASS for the reconcile contract assertions.

- [ ] **Step 5: Commit the reconcile workflow**

```bash
git add .github/workflows/main2main_reconcile.yaml .github/workflows/scripts/main2main_ci.py tests/main2main/test_main2main_workflow_contract.py
git commit -m "feat: add main2main reconcile workflow"
```

### Task 5: Rewrite `main2main_auto.yaml` around workflow-native modes

**Files:**
- Modify: `.github/workflows/main2main_auto.yaml`
- Modify: `.github/workflows/scripts/main2main_ci.py`
- Modify: `tests/main2main/test_main2main_workflow_contract.py`
- Modify: `tests/main2main/test_extract_and_analyze_contract.py`

- [ ] **Step 1: Add failing contract tests for the new mode split**

Add assertions for:

- `detect`
- `fix_phase2`
- `fix_phase3_prepare`
- `fix_phase3_finalize`
- short dispatch contracts (`pr_number`, `dispatch_token`, optional `bisect_run_id`)
- fix modes reading `e2e_run_id` from state instead of workflow input
- the old `mode=fixup` contract is removed or explicitly migrated

- [ ] **Step 2: Run the contract tests and confirm failures**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py
```

Expected: FAIL because `main2main_auto.yaml` still uses the old `mode=fixup` contract.

- [ ] **Step 3: Rewrite the detect path**

Update `main2main_auto.yaml` so `detect`:

- keeps the current dedup behavior
- creates the draft PR with `main2main`, `ready`, `ready-for-test`
- writes both `main2main-register` and `main2main-state`
- leaves E2E waiting to `main2main_reconcile.yaml`

- [ ] **Step 4: Rewrite the Phase 2 path**

Implement `fix_phase2` so it:

- validates `phase=2,status=fixing`
- records `fix_run_id`
- runs Claude error-analysis
- on changes: pushes and patches state to `phase=3,status=waiting_e2e`
- on no changes: preserves current semantics and also patches to `phase=3,status=waiting_e2e`

- [ ] **Step 5: Rewrite the Phase 3 prepare/finalize paths**

Implement:

- `fix_phase3_prepare`: compute `bisect-json`, mint token, dispatch `bisect_vllm.yaml`, write `waiting_bisect`
- `fix_phase3_finalize`: validate token + `bisect_run_id`, download `bisect_result.json`, run Claude, and transition to `phase=done,status=waiting_e2e` or terminal manual review

- [ ] **Step 6: Re-run workflow contract and extract/analyze tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

- [ ] **Step 7: Commit the auto workflow rewrite**

```bash
git add .github/workflows/main2main_auto.yaml .github/workflows/scripts/main2main_ci.py tests/main2main/test_main2main_workflow_contract.py tests/main2main/test_extract_and_analyze_contract.py
git commit -m "feat: migrate main2main auto workflow to native state machine"
```

### Task 6: Extend `bisect_vllm.yaml` for main2main callback mode without breaking standalone use

**Files:**
- Modify: `.github/workflows/bisect_vllm.yaml`
- Modify: `.github/workflows/scripts/bisect_helper.py`
- Modify: `tests/main2main/test_main2main_workflow_contract.py`
- Modify: `tests/main2main/test_extract_and_analyze_contract.py`

- [ ] **Step 1: Add failing tests for caller-mode separation**

Add tests that assert:

- `caller_type=standalone` is the default
- standalone bisect never dispatches `fix_phase3_finalize`
- `caller_type=main2main` dispatches finalize with only `pr_number`, `dispatch_token`, `bisect_run_id`
- `bisect_result.json` is uploaded in addition to the markdown summary

- [ ] **Step 2: Run the bisect/workflow contract tests and confirm failures**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: FAIL because `bisect_vllm.yaml` still behaves as a standalone-only workflow.

- [ ] **Step 3: Implement caller-type branching and callback-safe output**

Update `bisect_vllm.yaml` to:

- accept `caller_type`, `main2main_pr_number`, `main2main_dispatch_token`
- preserve existing standalone execution semantics
- emit `bisect_result.json`
- callback into `fix_phase3_finalize` only when `caller_type=main2main`
- use `main2main-bisect-pr-${main2main_pr_number}` concurrency for main2main-triggered runs

- [ ] **Step 4: Re-run the bisect/workflow contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

- [ ] **Step 5: Commit the bisect workflow changes**

```bash
git add .github/workflows/bisect_vllm.yaml .github/workflows/scripts/bisect_helper.py tests/main2main/test_main2main_workflow_contract.py tests/main2main/test_extract_and_analyze_contract.py
git commit -m "feat: add main2main-aware bisect callback flow"
```

## Chunk 3: Cutover and Regression

### Task 7: Execute workflow smoke validation and capture the operator checklist

**Files:**
- Modify: `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md`
- Modify: `docs/superpowers/plans/2026-04-07-main2main-workflow-native-orchestration.md`

- [ ] **Step 1: Write the exact smoke-test commands into the plan or adjacent notes**

Capture commands for:

- manual `detect`
- manual single-PR `reconcile`
- standalone `bisect_vllm.yaml`
- main2main-triggered phase3 bisect callback
- terminal `make_ready`
- terminal `manual_review`

- [ ] **Step 2: Run the smoke-test-compatible local verifications**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main
git -C /Users/antarctica/Work/PR/vllm-benchmarks diff --check
```

Expected: All tests PASS and `git diff --check` is clean.

- [ ] **Step 3: Document the GitHub-side smoke procedure**

Include the exact checks for:

- PR gets both comments
- `waiting_e2e` advances only from exact-head E2E runs
- `phase2 no_changes` reuses the already-failed E2E under phase3 semantics
- standalone bisect does not callback finalize
- `make_ready` and `manual_review` are idempotent

- [ ] **Step 4: Commit the finalized smoke-test notes before destructive cutover**

```bash
git add docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md docs/superpowers/plans/2026-04-07-main2main-workflow-native-orchestration.md
git commit -m "docs: finalize workflow-native main2main migration plan"
```

### Task 8: Remove the local control plane and obsolete deploy/test assets

**Files:**
- Delete: `main2main_orchestrator.py`
- Delete: `github_adapter.py`
- Delete: `service_main.py`
- Delete: `mcp_server.py`
- Delete: `terminal_worker.py`
- Delete: `state_store.py`
- Delete: `deploy/systemd/vllm-benchmarks-orchestrator.service`
- Delete: `deploy/systemd/orchestrator.env.example`
- Delete: `tests/main2main/test_orchestrator.py`
- Delete: `tests/main2main/test_github_adapter.py`
- Delete: `tests/main2main/test_service_main.py`
- Delete: `tests/main2main/test_mcp_server.py`
- Delete: `tests/main2main/test_state_store.py`
- Delete: `tests/main2main/test_deploy_assets.py`
- Delete: `tests/main2main/test_fixup_dispatch_contract.py`

- [ ] **Step 1: Add a failing safety test or grep check for removed runtime references**

Create or extend contract coverage so it fails if:

- workflow code imports deleted local modules
- deploy assets still mention `service_main.py`
- test files still assume `pending_terminal` or local MCP/service behavior
- old fixup-dispatch contract tests still assume `mode=fixup`

- [ ] **Step 2: Remove the local runtime files and stale deploy artifacts**

Delete the files listed above and update any remaining references in docs/tests/workflows.

- [ ] **Step 3: Re-run the surviving workflow-native test subset**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_ci.py \
  tests/main2main/test_main2main_workflow_contract.py \
  tests/main2main/test_bisect_helper_runtime_env.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

- [ ] **Step 4: Remove or rewrite the stale tests until the suite reflects only the workflow-native design**

Preferred replacements:

- keep `test_main2main_ci.py` for state machine semantics
- keep `test_main2main_workflow_contract.py` for workflow contracts
- keep bisect helper/extract tests for bisect/manual-review data flow

- [ ] **Step 5: Re-run the full `tests/main2main` suite**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q tests/main2main
```

Expected: PASS

- [ ] **Step 6: Commit the cutover/removal**

```bash
git add -A
git commit -m "refactor: remove local main2main control plane"
```
