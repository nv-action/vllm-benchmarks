# Proactive Upgrade Workflow

Systematically analyze upstream vLLM changes and adapt vllm-ascend before CI breaks.

## Step 1: Get Current vLLM Version

Find the vLLM version that vllm-ascend currently adapts to. Look in the **main branch** of `docs/source/community/versioning_policy.md` under the `Release compatibility matrix` section:

- **Current adapted commit**: e.g., `83b47f67b1dfad505606070ae4d9f83e50ad4ebd, v0.15.0 tag`
- **Compatible version**: e.g., `v0.15.0`

## Step 2: Get the Latest vLLM Code

Retrieve the latest commit from the local vLLM git repository:

```bash
cd ../vllm
git log -1 --format="%H %s"
```

If the vLLM repository is not found at the default location, prompt the user for the correct path.

## Step 3: Compare vLLM Changes

```bash
# File-level diff
git diff <old_commit> <new_commit> --name-only

# Commit log
git log --oneline <old_commit>..<new_commit>
```

## Step 4: Analyze Changes and Generate Report

Create `vllm_changes.md` to track changes relevant to vllm-ascend. Remove this file after all adaptation work is done.

### 4.1 Identify Key Source Files

Focus on files under `vllm/vllm/` directory:

```bash
git diff <old_commit> <new_commit> --name-only | grep -E "^vllm/" | head -200
git diff <old_commit> <new_commit> --name-only | wc -l
```

### 4.2 Categorize by Priority

| Priority | Category | Description |
|:---|:---|:---|
| **P0** | Breaking Changes | API changes that cause runtime errors if not adapted |
| **P1** | Important Changes | Changes affecting functionality or performance |
| **P2** | Moderate Changes | Changes that may need review for compatibility |
| **P3** | Model Changes | New models or model updates |
| **P4** | Minor Changes | Configuration, documentation, minor refactoring |

### 4.3 Useful Commands

```bash
# Breaking changes in commit messages
git log --oneline <old_commit>..<new_commit> | grep -iE "(refactor|breaking|api|rename|remove|deprecate)"

# Specific file changes
git diff <old_commit> <new_commit> -- <FILE_PATH>

# Renamed/moved files
git diff <old_commit> <new_commit> --name-status | grep -E "^R"

# Changes by key area
git diff <old_commit> <new_commit> -- vllm/platforms/
git diff <old_commit> <new_commit> -- vllm/model_executor/layers/fused_moe/
git diff <old_commit> <new_commit> -- vllm/model_executor/layers/attention/
git diff <old_commit> <new_commit> -- vllm/v1/worker/gpu/spec_decode/ vllm/config/speculative.py
```

### 4.4 Report Template

```markdown
# vLLM Changes Relevant to vLLM Ascend
# Generated: <DATE>
# Old commit: <OLD_COMMIT_HASH> (<OLD_VERSION>)
# New commit: <NEW_COMMIT_HASH>
# Total commits: <COUNT>

================================================================================
## P0 - Breaking Changes (Must Adapt)
================================================================================

### <INDEX>. <CHANGE_TITLE>
FILE: <VLLM_FILE_PATH>
CHANGE: <DESCRIPTION>
IMPACT: <WHAT_BREAKS_IF_NOT_ADAPTED>
VLLM_ASCEND_FILES:
  - <PATH_TO_ASCEND_FILE>

================================================================================
## P1 - Important Changes (Should Adapt)
================================================================================
...

================================================================================
## P2 - Moderate Changes (Review Needed)
================================================================================
...

================================================================================
## P3 - Model Changes
================================================================================
...

================================================================================
## P4 - Configuration/Minor Changes
================================================================================
...

================================================================================
## Files/Directories Renamed
================================================================================
<LIST_OF_RENAMED_FILES>

================================================================================
## END OF CHANGES
================================================================================
```

## Step 5: Adapt vllm-ascend

For each change listed in `vllm_changes.md`, evaluate whether adaptation is needed:

### 5.1 Internal Architecture Changes

- Check internal interfaces of vLLM core modules (scheduler, executor, model runner, etc.)
- Update Ascend-specific implementations (NPU worker/model runner, custom attention, custom ops)
- Preserve vllm-ascend specific modifications under `vllm_ascend/`
- Use the file mapping in SKILL.md to locate affected ascend files

### 5.2 Dependency Changes

- Check for dependency version changes in `pyproject.toml` or `setup.py`
- Update dependency declarations in vllm-ascend

### 5.3 Apply Fixes

Always use `vllm_version_is()` guards for backward compatibility (see SKILL.md). If you encounter specific runtime errors during testing (TypeError, AttributeError, etc.), refer to `reference/error-patterns.md` for concrete fix examples.

### 5.4 Update vLLM Commit References

After adapting code, update all vLLM commit references from the old commit to the new commit:

```bash
grep -Frl "<OLD_COMMIT>" . | xargs sed -i '' "s/<OLD_COMMIT>/<NEW_COMMIT>/g"

# Verify no old references remain
grep -Frn "<OLD_COMMIT>" .
```

## Step 6: Output Summary

Output a structured summary in the conversation. This summary serves as the skill's primary output — it's what a Workflow consumes, and what gets used as PR body content in standalone mode.

```markdown
### Proactive Upgrade Summary

**Commit range:** `<OLD_COMMIT_SHORT>`..`<NEW_COMMIT_SHORT>`

#### Changes Adapted
| Priority | Change | vLLM File | vllm-ascend File | Description |
|:---|:---|:---|:---|:---|
| P0 | `<change title>` | `<vllm path>` | `<ascend path>` | `<what was done>` |

#### Files Changed
- `<file list>`
```

Also keep the `vllm_changes.md` report file in the repo root until all adaptation work is complete.
