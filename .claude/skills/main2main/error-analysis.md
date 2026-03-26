# CI Failure Diagnosis Workflow

Diagnose and fix vLLM-Ascend CI failures caused by upstream vLLM main branch evolution. This implements a 4-phase pipeline: log mining, change analysis, report generation, and automated fix.

## Prerequisites (run these first, before Phase 1)

Run each check now. If any fails, stop and resolve it before continuing.

```bash
gh auth status                      # Must show "Logged in to github.com"
git rev-parse --show-toplevel       # Must end with "vllm-ascend"
ls ../vllm 2>/dev/null || echo "vLLM repo not found at ../vllm"
```

If the vLLM repo is not at `../vllm`, ask the user for its path before proceeding.

Before Phase 2, ensure the vllm repo has both the good and bad commits:

```bash
git cat-file -t <GOOD_COMMIT>  # Should print "commit"
git cat-file -t <BAD_COMMIT>   # Should print "commit"
```

## Token Budget Strategy

CI logs can be enormous (10K+ lines per job). To avoid exhausting context:

1. **Always use the repo's CI log analysis script** for Phase 1. It processes logs in a subprocess and returns only structured results.
2. **Write a partial report early.** After Phase 1, immediately write a skeleton `vllm_error_analyze.md` with the Overview table. Fill in upstream commit details as you complete Phase 2.
3. **Use the local vLLM repo** for all upstream code analysis. Run `git log`, `git diff`, `git show` directly — faster and more reliable than GitHub API calls.
4. **If falling back to manual mode**, never pipe full logs into context. Always filter through `grep` with `head` limits first.

---

## Phase 1: Fault Context Acquisition

### 1.1 Run the CI Log Analysis Script

The analysis script lives in the vllm-ascend repo itself. Confirm it exists:

```bash
ls .github/workflows/scripts/ci_log_summary.py
```

Then run:

```bash
# With a specific run ID:
python3 .github/workflows/scripts/ci_log_summary.py --run-id <RUN_ID> --format llm-json --output /tmp/ci_analysis.json

# Or auto-find latest failed run (omit --run-id — the script will find it):
# Note: --run-id is required in the current script. If no run ID is provided,
# use `gh run list -w schedule_test_vllm_main.yaml -s failure -L 1 --json databaseId -q '.[0].databaseId'`
# to find the latest failed run ID first.
```

The script will:
- Find the failed workflow run and download logs for each failed job
- Extract the **bad commit** from the vLLM version string in logs
- Extract the **good commit** from `.github/workflows/pr_test_full.yaml`
- Parse all failed test file paths and individual test case identifiers
- Extract root-cause exceptions (TypeError, AttributeError, ImportError, etc.)
- Skip wrapper errors and downstream effects
- Detect environment flakes (Stale file handle, ConnectionResetError, filelock errors)
- Deduplicate errors by normalized signature
- Output a structured JSON report optimized for LLM analysis (`--format llm-json`)

### 1.2 Read the Script Output

Load `/tmp/ci_analysis.json` and extract:

```json
{
  "run_id": 12345678,
  "run_url": "https://github.com/vllm-project/vllm-ascend/actions/runs/12345678",
  "good_commit": "15d76f74e2fdb12a95ea00f0ca283acf6219a2b7",
  "bad_commit": "6d4f9d3ad5aa3750697edcf013ad080619ae25e9",
  "failed_test_files_count": 3,
  "failed_test_cases_count": 5,
  "failed_test_files": ["tests/e2e/test_basic_correctness.py"],
  "failed_test_cases": ["tests/e2e/test_basic_correctness.py::test_chunked_prefill"],
  "code_bugs": [
    {"error_type": "TypeError", "error_message": "...", "failed_test_files": [...], "failed_test_cases": [...], "context": [...]}
  ],
  "env_flakes": [
    {"error_type": "OSError", "error_message": "Stale file handle", "failed_test_files": [...], "failed_test_cases": [...]}
  ]
}
```

**Phase 1 outputs:** `RUN_ID`, `GOOD_COMMIT`, `BAD_COMMIT`, list of `code_bugs`, list of `env_flakes`, list of `failed_test_files`, list of `failed_test_cases`.

---

## Phase 2: Change Comparison & Adaptation Analysis

Map each code bug to the specific upstream vLLM commit that caused it. Only analyze `code_bugs`, not `env_flakes`.

All commands run against the **local vLLM repo**.

### 2.1 Get the Commit Diff

```bash
git diff <GOOD_COMMIT>..<BAD_COMMIT> --name-only
git log --oneline <GOOD_COMMIT>..<BAD_COMMIT>
```

Focus on the critical paths listed in the SKILL.md Key Areas section.

### 2.2 Root Cause Correlation

For each code bug from the script output, use the error type, message, and context to figure out how upstream changes caused it. Find the commit(s) that introduced the relevant change, then analyze the code diff to understand why it breaks vllm-ascend.

### 2.3 File Impact Mapping

Use the vLLM-to-vllm-ascend file mapping table in SKILL.md to identify which ascend files need changes.

**Phase 2 outputs:** For each code bug: the causal upstream commit(s), the changed vLLM file(s), and the affected vllm-ascend file(s).

---

## Phase 3: Generate Diagnostic Report

Write `vllm_error_analyze.md` in the repository root **as early as possible** — start right after Phase 1. Use the script output JSON; do not re-download logs.

```markdown
# vLLM-Ascend CI Failure Analysis Report

## Overview

| Item                      | Value                      |
| :------------------------ | :------------------------- |
| **Run URL**               | <url>                      |
| **Run Date**              | <date>                     |
| **Good Commit (pinned)**  | `<good_commit>`            |
| **Bad Commit (tested)**   | `<bad_commit>`             |
| **Total Failed Jobs**     | X / Y                      |
| **Distinct Issues Found** | N code bugs + M env flakes |

## Failed Jobs Summary

| Job        | Conclusion | Failed Tests     |
|:---        |:---        |:---              |
| <job_name> | failure    | <test1>, <test2> |

## Issue Analysis

### Issue 1: <Short Description>

| Item                      | Detail                                   |
| :------------------------ | :--------------------------------------- |
| **Category**              | Code Bug / Environment Flake             |
| **Error Type**            | <exception class>                        |
| **Affected Tests**        | <list>                                   |
| **Root Cause Commit**     | `<sha>` — "<commit message>" (<PR link>) |
| **Changed File**          | `<vllm file path>`                       |
| **Impact in vllm-ascend** | `<ascend file path>`                     |

**Error Traceback:**
(use context from script output)

**Explanation:** <Why this change breaks vllm-ascend>

**Fix Suggestion:** <Specific code change needed>

### Issue 2: ...

## Summary Table

| #    | Error | Category | Upstream Commit | Affected Tests | Fix  |
| :--- | :---- | :------- | :-------------- | :------------- | :--- |

## Recommended Actions

1. <action item>
```

---

## Phase 4: Fix & Submit

### 4.1 Apply Fixes

Only fix `Code Bug` issues. Skip `Environment Flake` issues entirely.

Refer to `reference/error-patterns.md` for common fix patterns with concrete examples.

### 4.2 Version Compatibility

Most fixes require `vllm_version_is()` guards — see the pattern in SKILL.md.

### 4.3 Update vLLM Commit References

After applying code fixes, update all vllm commit references from the good commit to the bad commit:

```bash
grep -Frl "<GOOD_COMMIT>" . | xargs sed -i '' "s/<GOOD_COMMIT>/<BAD_COMMIT>/g"

# Verify no old references remain
grep -Frn "<GOOD_COMMIT>" .
```

### 4.4 Output Fix Summary

After all fixes are applied, output a structured summary in the conversation. This summary serves as the skill's primary output — it's what a Workflow consumes, and what gets used as PR body content in standalone mode.

```markdown
### CI Fix Summary (run ID: <RUN_ID>)

**Commit range:** `<GOOD_COMMIT_SHORT>`..`<BAD_COMMIT_SHORT>`

#### Issues Fixed
| Error | Upstream Cause Commit | Affected Files | Fix Description |
|:---|:---|:---|:---|
| `TypeError: forward_oot() got unexpected kwarg 'X'` | `abc1234` — "refactor attention API" | `vllm_ascend/attention/` | Added `vllm_version_is()` guard |

#### Issues Skipped (Environment Flakes)
- `OSError: Stale file handle` — no code fix needed

#### Files Changed
- `vllm_ascend/attention/...`
- `.github/workflows/...`
```

The "Upstream Cause Commit" column is critical — it links each fix back to the specific vLLM commit that caused the breakage, identified during Phase 2.
