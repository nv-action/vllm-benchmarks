# vLLM-Benchmarks CI Failure Analysis Report

## Overview

| Item                      | Value                                      |
| :------------------------ | :----------------------------------------- |
| **Run URL**               | https://github.com/nv-action/vllm-benchmarks/actions/runs/23576154439 |
| **Run Date**              | 2026-03-26                                 |
| **Good Commit (pinned)**  | `4034c3d32e30d01639459edd3ab486f56993876d` |
| **Bad Commit (tested)**   | `e2db2b42347ae27da3083c530c71b251861ed220` |
| **Total Failed Jobs**     | 8 / 18                                     |
| **Distinct Issues Found** | 1 configuration bug                        |

## Failed Jobs Summary

| Job                                         | Conclusion | Failed Step                          |
|:--------------------------------------------|:-----------|:-------------------------------------|
| e2e-full (v0.18.0) / multicard-2-full (0)   | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (v0.18.0) / multicard-4-full (0)   | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (v0.18.0) / singlecard-full (0)    | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (v0.18.0) / singlecard-full (1)    | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (ed359c49) / multicard-2-full (0)  | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (ed359c49) / multicard-4-full (0)  | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (ed359c49) / singlecard-full (0)   | failure    | Install vllm-project/vllm-ascend     |
| e2e-full (ed359c49) / singlecard-full (1)   | cancelled  | Install vllm-project/vllm-ascend     |

## Issue Analysis

### Issue 1: Git Safe Directory Configuration Error

| Item                      | Detail                                                      |
| :------------------------ | :---------------------------------------------------------- |
| **Category**              | Configuration Bug                                           |
| **Error Type**            | Git safe directory ownership error                          |
| **Affected Tests**        | All E2E test jobs                                           |
| **Root Cause**            | Workflow files use hardcoded path `/__w/vllm-ascend/vllm-ascend` but actual path is `/__w/vllm-benchmarks/vllm-benchmarks` |
| **Changed Files**         | `.github/workflows/_e2e_test.yaml`, `.github/workflows/_unit_test.yaml`, `.github/workflows/_pre_commit.yml`, `.github/workflows/labled_download_model.yaml`, `.github/workflows/_e2e_nightly_single_node_models.yaml` |

**Error Traceback:**
```
ERROR    fatal: detected dubious ownership in     _git.py:32
         repository at
         '/__w/vllm-benchmarks/vllm-benchmarks'
         To add an exception for this directory,
         call:

             git config --global --add
         safe.directory
         /__w/vllm-benchmarks/vllm-benchmarks
git introspection failed: fatal: detected dubious ownership in repository at '/__w/vllm-benchmarks/vllm-benchmarks'

error: The build backend returned an error
  Caused by: Call to `setuptools.build_meta.build_editable` failed (exit status: 1)
```

**Explanation:**
The workflow files were copied from the `vllm-ascend` repository and contain hardcoded paths `/__w/vllm-ascend/vllm-ascend` for the git safe directory configuration. However, in the `vllm-benchmarks` repository, the actual workspace path is `/__w/vllm-benchmarks/vllm-benchmarks`.

During package installation, `vcs_versioning` (used for version introspection) tries to run git commands in the repository, but git rejects them because the safe.directory configuration points to the wrong path.

**Fix:**
Replace hardcoded paths with `$GITHUB_WORKSPACE` environment variable, which is portable and works correctly regardless of the repository name.

## Summary Table

| # | Error | Category | Root Cause | Fix |
| :--- | :---- | :------- | :-------------- | :--- |
| 1 | Git safe directory error | Config Bug | Hardcoded path `/__w/vllm-ascend/vllm-ascend` | Changed to `$GITHUB_WORKSPACE` |

## Recommended Actions

1. **Apply the fix** - Replace all hardcoded safe.directory paths with `$GITHUB_WORKSPACE` (done)
2. **Review other workflows** - Ensure no other files have similar hardcoded paths from the original repo
3. **Consider a shared pattern** - Use `$GITHUB_WORKSPACE` consistently across all workflow files

## Bisect Analysis

The bisect workflow run (23577242773) failed due to a configuration issue:
- The test commands (`tests/test_regex.py`, etc.) don't exist in the vllm-benchmarks repository
- The bisect workflow is configured for vllm-ascend test patterns, not vllm-benchmarks

**Bisect Run Status:** Failed - artifact upload conflict (409 Conflict)
**Root Cause:** Test command patterns don't match any files in this repository

## Latest CI Status

| Run ID | Name | Status | Notes |
|:-------|:-----|:-------|:------|
| 23577354973 | Main2Main Auto | success | Fix verified working |
| 23577235270 | Main2Main Auto fixup | in_progress | Phase 3 fixup |

## Summary

The git safe.directory fix has been successfully applied and verified. The Main2Main Auto run (23577354973) completed successfully after the fix was committed.

### Files Changed

- `.github/workflows/_e2e_test.yaml` - 7 occurrences fixed
- `.github/workflows/_unit_test.yaml` - 1 occurrence fixed
- `.github/workflows/_pre_commit.yml` - 3 occurrences fixed
- `.github/workflows/labled_download_model.yaml` - 1 occurrence fixed
- `.github/workflows/_e2e_nightly_single_node_models.yaml` - 1 occurrence fixed

### Commit

- `b18720c2` - fix: use $GITHUB_WORKSPACE for git safe.directory config