# Main2Main Auto Pipeline

The main2main auto pipeline (`main2main_auto.yaml`) automatically adapts vllm-ascend to upstream vLLM main branch changes using AI-assisted code generation.

## Overview

The pipeline runs daily and consists of three phases:

1. **Phase 1 — Detect & Adapt**: Compares the currently pinned vLLM commit against vLLM main. If changes exist, Claude Code analyzes the diffs and proactively applies adaptation fixes, then creates a draft PR.

2. **Phase 2 — Test & Fix Loop**: Triggers the full test suite on the PR branch. If tests fail, `extract_failures.py --deep` extracts root-cause errors and Claude Code applies targeted fixes. Repeats up to `max_phase2_retries` rounds (default: 2).

3. **Phase 3 — Bisect & Fix**: If failures persist after Phase 2, triggers `bisect_vllm.yaml` to identify culprit commits. Claude Code then generates targeted fixes based on the bisection results.

A final **Report** job marks the PR as ready-for-review on success, or creates a GitHub Issue for manual review on failure.

## Required Secrets

| Secret              | Description             |
| :------------------ | :---------------------- |
| `ANTHROPIC_API_KEY` | API key for Claude Code |

The workflow also uses `github.token` (automatically provided) for GitHub API operations.

## Required Permissions

The workflow needs these repository permissions:

- `contents: write` — push commits to the PR branch
- `pull-requests: write` — create and modify PRs
- `issues: write` — create issues on failure
- `actions: write` — trigger test and bisect workflows

## Manual Trigger

Trigger via GitHub Actions UI or CLI:

```bash
# Use defaults (latest vLLM main, 2 retries)
gh workflow run main2main_auto.yaml

# Target a specific vLLM commit with custom retries
gh workflow run main2main_auto.yaml \
  -f target_commit="abc123def456" \
  -f max_phase2_retries=3 \
  -f max_phase3_retries=2
```

## Monitoring

- **Workflow runs**: Check the Actions tab for `Main2Main Auto`
- **Phase 2 logs**: Look for `::group::Phase 2 Round N/M` markers
- **Cost tracking**: Each Claude Code invocation has `--max-budget-usd` caps:
  - Phase 1: $10.00
  - Phase 2: $5.00 per round
  - Phase 3: $5.00

## Cost Expectations

| Phase     | Budget Cap     | Typical Usage        |
| :-------- | :------------- | :------------------- |
| Phase 1   | $10.00         | $2-5 for small diffs |
| Phase 2   | $5.00 × rounds | $3-5 per round       |
| Phase 3   | $5.00          | $2-4                 |
| **Total** | ~$25 max       | **$5-15 typical**    |

## How It Works

### Phase 1: Detect & Adapt

1. Reads the pinned vLLM commit from `docs/source/community/versioning_policy.md`
2. Fetches latest vLLM main commit via GitHub API
3. If different, creates a branch and invokes Claude Code with the `main2main` skill
4. Claude analyzes the diff, applies fixes, updates commit references
5. Creates a draft PR

### Phase 2: Test & Fix Loop

1. Triggers `schedule_test_vllm_main.yaml` on the PR branch
2. Waits for completion with `gh run watch`
3. On failure, runs `extract_failures.py --deep` for root-cause extraction
4. Invokes Claude Code with failures JSON, using `--resume` to maintain context
5. Repeats until all tests pass or max retries reached

### Phase 3: Bisect & Fix

1. Triggers `bisect_vllm.yaml` with the remaining failures
2. Downloads bisect results identifying culprit commits
3. Invokes Claude Code with culprit context for targeted fixes
4. Runs a final verification test
