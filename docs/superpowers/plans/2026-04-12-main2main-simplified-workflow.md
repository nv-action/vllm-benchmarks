# Main2Main Simplified Workflow Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite main2main into one top-level workflow with one long-lived 4-card main job that runs detect, direct test, up to 3 fix rounds, then up to 2 bisect-fix rounds by triggering the separate bisect workflow through `gh workflow run`, and finally creates one draft PR plus an optional manual-review issue.

**Architecture:** Keep all adaptation state in one main job inside `.github/workflows/schedule_main2main_auto.yaml`. Keep `.github/workflows/dispatch_main2main_bisect.yaml` as a standalone `workflow_dispatch` workflow. Do not use `workflow_call`, do not split the main flow across jobs, and do not use `git bundle`.

**Tech Stack:** GitHub Actions YAML, Python 3 helper CLI, `gh`, `git`, existing `run_suite.py`, existing `ci_log_summary.py`, `tools/bisect_helper.py`, `tools/bisect_vllm.sh`, and pytest-based workflow/script contract tests.

---

## File Structure

### Create

- `.github/workflows/scripts/main2main_simplified.py`
- `tests/main2main/test_main2main_simplified.py`

### Modify

- `.github/workflows/schedule_main2main_auto.yaml`
- `.github/workflows/dispatch_main2main_bisect.yaml`
- `.github/workflows/scripts/ci_log_summary.py`
- `tests/main2main/test_main2main_workflow_contract.py`
- `tests/main2main/test_extract_and_analyze_contract.py`

### Verify Only Unless Tests Force a Change

- `.github/workflows/scripts/run_suite.py`
- `.github/workflows/scripts/config.yaml`

The fixed suite remains `e2e-main2main`. Do not broaden `run_suite.py` or `config.yaml` unless a test proves the current contract is insufficient.

## Chunk 0: Workspace Safety

### Task 0: Use the existing isolated worktree

**Files:**

- Verify only: `/Users/antarctica/Work/PR/vllm-benchmarks-main2main-simplified`

- [ ] **Step 1: Confirm the implementation worktree exists**

Run:

```bash
git -C /Users/antarctica/Work/PR/vllm-benchmarks-main2main-simplified status --short
```

Expected: the worktree exists and is usable for implementation.

- [ ] **Step 2: Perform all remaining edits in the isolated worktree**

Every subsequent edit, test, and commit in this plan should run from:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks-main2main-simplified
```

## Chunk 1: Helper CLI and Summary Contracts

### Task 1: Add a small helper CLI for deterministic workflow decisions

**Files:**

- Create: `.github/workflows/scripts/main2main_simplified.py`
- Create: `tests/main2main/test_main2main_simplified.py`
- Modify: `tests/main2main/test_extract_and_analyze_contract.py`
- Modify if needed: `.github/workflows/scripts/ci_log_summary.py`

- [ ] **Step 1: Write failing tests for helper decisions and output rendering**

Create `tests/main2main/test_main2main_simplified.py` with focused tests covering:

```python
def test_extract_bisect_test_cmd_prefers_failed_test_cases():
    ...

def test_extract_bisect_test_cmd_falls_back_to_failed_test_files():
    ...

def test_collect_new_commits_renders_short_sha_and_full_message():
    ...

def test_render_pr_body_includes_commit_range_and_commit_log():
    ...

def test_render_manual_review_issue_includes_pr_url_and_bisect_summary():
    ...

def test_build_bisect_request_id_is_unique_and_stable_shape():
    ...
```

- [ ] **Step 2: Add a failing local-log contract test for `ci_log_summary.py --format llm-json`**

Extend `tests/main2main/test_extract_and_analyze_contract.py` with a focused test that:

- writes a small synthetic local pytest log
- invokes `ci_log_summary.py --log-file ... --format llm-json`
- asserts the JSON includes:
    - `failed_test_files`
    - `failed_test_cases`
    - `code_bugs`
    - `env_flakes`

- [ ] **Step 3: Run the helper and summary tests to confirm they fail**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_simplified.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: FAIL because the helper script does not yet exist and the local log contract may need tightening.

- [ ] **Step 4: Implement `.github/workflows/scripts/main2main_simplified.py`**

Implement pure functions and shell-friendly subcommands for:

- loading summary JSON
- extracting a deterministic bisect test command
- building a `request_id`
- finding the correct bisect run by `request_id`
- polling a bisect run to completion
- collecting commits created since a base ref
- rendering PR body markdown
- rendering manual-review issue markdown

Example CLI shape:

```bash
python3 .github/workflows/scripts/main2main_simplified.py extract-bisect-test-cmd ...
python3 .github/workflows/scripts/main2main_simplified.py build-request-id ...
python3 .github/workflows/scripts/main2main_simplified.py find-bisect-run ...
python3 .github/workflows/scripts/main2main_simplified.py render-pr-body ...
python3 .github/workflows/scripts/main2main_simplified.py render-manual-review-issue ...
```

- [ ] **Step 5: Tighten `ci_log_summary.py` only if the new contract test fails**

Make the minimum change needed so local-log + `llm-json` consistently returns the workflow-critical fields.

- [ ] **Step 6: Re-run the helper and summary tests until they pass**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_simplified.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

- [ ] **Step 7: Commit Chunk 1**

```bash
git add .github/workflows/scripts/main2main_simplified.py \
  tests/main2main/test_main2main_simplified.py \
  tests/main2main/test_extract_and_analyze_contract.py \
  .github/workflows/scripts/ci_log_summary.py
git commit -m "feat: add simplified main2main helper contracts"
```

## Chunk 2: Bisect Workflow Contract

### Task 2: Simplify `dispatch_main2main_bisect.yaml` to a `workflow_dispatch` executor

**Files:**

- Modify: `.github/workflows/dispatch_main2main_bisect.yaml`
- Modify: `tests/main2main/test_main2main_workflow_contract.py`

- [ ] **Step 1: Rewrite failing workflow contract tests for the simplified bisect interface**

Update `tests/main2main/test_main2main_workflow_contract.py` so bisect assertions reflect the new design:

- `workflow_dispatch` must exist
- required inputs must include:
    - `good_commit`
    - `bad_commit`
    - `test_cmd`
    - `request_id`
- old main2main callback/state inputs must be removed from the active path:
    - `caller_type`
    - `caller_run_id`
    - `main2main_pr_number`
    - `main2main_dispatch_token`
- callback/reconcile job must be absent

- [ ] **Step 2: Run the workflow contract test and confirm it fails**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py -k bisect
```

Expected: FAIL because the current bisect workflow still contains old callback/state semantics.

- [ ] **Step 3: Refactor `.github/workflows/dispatch_main2main_bisect.yaml`**

Implement these changes:

- keep `workflow_dispatch`
- reduce active inputs to:
    - `good_commit`
    - `bad_commit`
    - `test_cmd`
    - `request_id`
- remove callback-main2main logic
- remove reconcile/dispatch-token behavior
- keep matrix-building and aggregation logic
- make the run/artifact naming include `request_id` so the main workflow can find the right run

- [ ] **Step 4: Ensure the bisect workflow always publishes deterministic artifacts**

Upload at least:

- `bisect_result.json`
- `bisect_summary.md`

Artifact naming must include `request_id`.

- [ ] **Step 5: Re-run the bisect workflow contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py -k bisect
```

Expected: PASS

- [ ] **Step 6: Commit Chunk 2**

```bash
git add .github/workflows/dispatch_main2main_bisect.yaml \
  tests/main2main/test_main2main_workflow_contract.py
git commit -m "refactor: simplify main2main bisect workflow dispatch"
```

## Chunk 3: Main Workflow Rewrite

### Task 3: Rewrite `schedule_main2main_auto.yaml` around one long-lived main job

**Files:**

- Modify: `.github/workflows/schedule_main2main_auto.yaml`
- Modify if needed: `.github/workflows/scripts/main2main_ci.py`

- [ ] **Step 1: Rewrite failing workflow contract tests for the simplified main flow**

Update `tests/main2main/test_main2main_workflow_contract.py` so the main workflow contract asserts:

- only `schedule` and `workflow_dispatch` remain
- only `target_commit` remains as a dispatch input
- old state-machine inputs are absent
- the active path uses one main 4-card job
- no reconcile callback path is present

- [ ] **Step 2: Run the main workflow contract tests and confirm they fail**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py -k auto
```

Expected: FAIL because the current workflow still reflects the old multi-phase model.

- [ ] **Step 3: Rewrite `.github/workflows/schedule_main2main_auto.yaml`**

Implement one long-lived main job with visible stages:

- prepare
- detect
- initial test
- fix loop
- bisect dispatch + poll + download
- bisect-fix loop
- finalize

Required behavior:

- use the 4-card environment only once
- run the fixed `e2e-main2main` suite
- allow up to 3 fix rounds
- allow up to 2 bisect-fix rounds
- commit when code changes, but do not push until finalize
- call `gh workflow run dispatch_main2main_bisect.yaml` when bisect is needed
- poll the correct bisect run using `request_id`
- download `bisect_result.json`
- create one draft PR at the end
- optionally create one manual-review issue at the end

- [ ] **Step 4: Keep helper logic out of raw YAML where it becomes unreadable**

If inline shell becomes hard to maintain, move only deterministic logic into `.github/workflows/scripts/main2main_simplified.py`. Keep the stage flow visible in YAML.

- [ ] **Step 5: Re-run the main workflow contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_workflow_contract.py -k auto
```

Expected: PASS

- [ ] **Step 6: Commit Chunk 3**

```bash
git add .github/workflows/schedule_main2main_auto.yaml \
  .github/workflows/scripts/main2main_ci.py \
  .github/workflows/scripts/main2main_simplified.py \
  tests/main2main/test_main2main_workflow_contract.py
git commit -m "refactor: rewrite main2main auto workflow"
```

## Chunk 4: Polling, PR Body, and Manual Review Integration

### Task 4: Finish the end-to-end orchestration details

**Files:**

- Modify: `.github/workflows/schedule_main2main_auto.yaml`
- Modify: `.github/workflows/scripts/main2main_simplified.py`
- Modify tests as needed

- [ ] **Step 1: Add focused tests for bisect run lookup and zero-commit finalize behavior**

Extend `tests/main2main/test_main2main_simplified.py` to cover:

- matching the correct bisect run by `request_id`
- refusing stale or mismatched runs
- zero-commit drift path renders no PR body
- commit log renders `short sha + full commit message` in chronological order

- [ ] **Step 2: Implement the bisect polling contract**

The helper or workflow logic must:

- trigger bisect with `gh workflow run`
- discover the matching run via `gh run list` / `gh run view`
- wait until completion
- fail closed if the matching run never appears or ends unsuccessfully
- download the artifact that contains `bisect_result.json`

- [ ] **Step 3: Implement final PR and issue rendering**

Use helper CLI commands to render:

- final PR body
- manual-review issue body

Rules:

- no drift -> no PR, no issue
- drift but zero new commits -> no PR, no issue
- drift with new commits and final pass -> push + draft PR
- drift with new commits and final fail -> push + draft PR + manual-review issue

- [ ] **Step 4: Run the helper tests again**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_simplified.py
```

Expected: PASS

- [ ] **Step 5: Commit Chunk 4**

```bash
git add .github/workflows/schedule_main2main_auto.yaml \
  .github/workflows/scripts/main2main_simplified.py \
  tests/main2main/test_main2main_simplified.py
git commit -m "feat: add bisect polling and finalization flow"
```

## Chunk 5: Verification

### Task 5: Run targeted repo tests first

**Files:**

- Verify only

- [ ] **Step 1: Run workflow and helper contract tests**

Run:

```bash
/Users/antarctica/Work/PR/vllm-benchmarks/.venv/bin/pytest -q \
  tests/main2main/test_main2main_simplified.py \
  tests/main2main/test_main2main_workflow_contract.py \
  tests/main2main/test_extract_and_analyze_contract.py
```

Expected: PASS

### Task 6: Run lightweight static validation

- [ ] **Step 2: Validate YAML and Python syntax**

Run:

```bash
python3 -m compileall .github/workflows/scripts/main2main_simplified.py
python3 - <<'PY'
import yaml
for path in [
    ".github/workflows/schedule_main2main_auto.yaml",
    ".github/workflows/dispatch_main2main_bisect.yaml",
]:
    with open(path, "r", encoding="utf-8") as f:
        yaml.safe_load(f)
    print("OK", path)
PY
```

Expected:

```text
OK .github/workflows/schedule_main2main_auto.yaml
OK .github/workflows/dispatch_main2main_bisect.yaml
```

### Task 7: Manual workflow validation

- [ ] **Step 3: Run a no-drift scenario**

Verify:

- workflow exits early
- no PR is created

- [ ] **Step 4: Run a drift scenario that finishes before bisect**

Verify:

- detect and fix happen in one main job
- environment setup is not repeated
- commits are created locally before final push
- one draft PR is created at the end

- [ ] **Step 5: Run a drift scenario that requires bisect**

Verify:

- the main job triggers `dispatch_main2main_bisect.yaml` through `gh workflow run`
- the main job finds the correct bisect run by `request_id`
- `bisect_result.json` is downloaded and consumed in the same main job
- if the final result passes, one draft PR is created

- [ ] **Step 6: Run a failure scenario that ends in manual review**

Verify:

- the main job consumes the full fix and bisect budgets
- the final result is `push + draft PR + manual-review issue`

## Final Verification Checklist

- [ ] Main workflow no longer depends on reconcile/state comments/dispatch token
- [ ] Main workflow uses one long-lived 4-card job
- [ ] Bisect workflow remains separate and `workflow_dispatch` based
- [ ] Main job triggers bisect through `gh workflow run`
- [ ] No cross-job git state transfer exists
- [ ] Fixed suite remains `e2e-main2main`
- [ ] `ci_log_summary.py --log-file --format llm-json` returns required fields
- [ ] PR body includes `short sha + full commit message`
- [ ] Manual-review issue is created only on final failure

## Suggested Commit Sequence

1. `feat: add simplified main2main helper contracts`
2. `refactor: simplify main2main bisect workflow dispatch`
3. `refactor: rewrite main2main auto workflow`
4. `feat: add bisect polling and finalization flow`
