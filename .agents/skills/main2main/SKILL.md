---
name: main2main
description: "Adapts vllm-ascend to the latest vLLM main branch. Works in interactive mode (local session) and CI mode (GitHub Actions)."
---

# main2main Skill

Adapt vllm-ascend to track upstream vLLM main branch changes.

## Mode Detection

Check for the environment variable `MAIN2MAIN_CI_MODE`:
- **If set:** CI mode — use GitHub API for vLLM diffs, skip prompts, commit without PR
- **If unset:** Interactive mode — use local vLLM repo, ask user when unclear

## Step 1: Get Current vLLM Version

Read `docs/source/community/versioning_policy.md` under the Release compatibility matrix.
Extract:
- **OLD_COMMIT**: the 40-char hash in the `main` row's vLLM column
- **COMPATIBLE_VERSION**: e.g., `v0.15.0`

## Step 2: Get Target vLLM Commit

**CI mode:** Read from `NEW_COMMIT` env var, or fetch latest:
```bash
gh api repos/vllm-project/vllm/commits/main --jq '.sha'
```

**Interactive mode:** Use local vLLM repo:
```bash
cd ../vllm && git log -1 --format="%H %s"
```

If OLD_COMMIT == NEW_COMMIT, exit early — no update needed.

## Step 3: Analyze vLLM Changes

Get the diff between old and new commits:

**CI mode (GitHub API):**
```bash
gh api "/repos/vllm-project/vllm/compare/${OLD_COMMIT}...${NEW_COMMIT}" \
  --jq '.files[] | {filename, status, previous_filename}' | head -200
```

**Interactive mode (local git):**
```bash
cd ../vllm && git diff ${OLD_COMMIT} ${NEW_COMMIT} --name-only
```

### 3.1 Key Areas to Focus On

When analyzing vLLM changes, pay special attention to these areas that typically require vLLM Ascend adaptation:

1. **Platform Interface** (`vllm/platforms/`)
   - New abstract methods that must be implemented
   - Method signature changes
   - New platform features

2. **MoE (Mixture of Experts)** (`vllm/model_executor/layers/fused_moe/`)
   - FusedMoE layer changes
   - Activation function changes
   - Router changes

3. **Attention** (`vllm/model_executor/layers/attention/`)
   - Attention backend changes
   - New parameters or interfaces
   - MLA (Multi-Head Latent Attention) updates

4. **Speculative Decoding** (`vllm/v1/worker/gpu/spec_decode/`, `vllm/config/speculative.py`)
   - Import path changes
   - Config field changes
   - New speculative methods

5. **Distributed** (`vllm/distributed/`)
   - Parallel state changes
   - KV transfer changes
   - Device communicator updates

6. **Models** (`vllm/model_executor/models/`)
   - New model architectures
   - Model interface changes

7. **Worker/Model Runner** (`vllm/v1/worker/gpu/model_runner.py`)
   - New worker methods
   - Model runner changes

8. **Quantization** (`vllm/model_executor/layers/quantization/`)
   - Quantization config changes
   - compress-tensor method changes

### 3.2 Categorize Changes by Priority

| Priority | Category          | Description                                               |
| -------- | ----------------- | --------------------------------------------------------- |
| **P0**   | Breaking Changes  | API changes that will cause runtime errors if not adapted |
| **P1**   | Important Changes | Changes that affect functionality or performance          |
| **P2**   | Moderate Changes  | Changes that may need review for compatibility            |
| **P3**   | Model Changes     | New models or model updates                               |
| **P4**   | Minor Changes     | Configuration, documentation, or minor refactoring        |

### 3.3 Check for Renamed/Moved Files

**CI mode:**
```bash
gh api "/repos/vllm-project/vllm/compare/${OLD_COMMIT}...${NEW_COMMIT}" \
  --jq '.files[] | select(.status == "renamed") | {old: .previous_filename, new: .filename}'
```

**Interactive mode:**
```bash
cd ../vllm && git diff ${OLD_COMMIT} ${NEW_COMMIT} --name-status | grep -E "^R"
```

### 3.4 Generate Change Report

Create `vllm_changes.md` with changes organized by priority (P0-P4). Use the template:

```markdown
# vLLM Changes Relevant to vLLM Ascend
# Old commit: <OLD_COMMIT_HASH>
# New commit: <NEW_COMMIT_HASH>

## P0 - Breaking Changes (Must Adapt)

### 1. <CHANGE_TITLE>
FILE: <VLLM_FILE_PATH>
CHANGE: <DESCRIPTION_OF_CHANGE>
IMPACT: <WHAT_BREAKS_IF_NOT_ADAPTED>
VLLM_ASCEND_FILES:
  - <PATH_TO_ASCEND_FILE>

## P1 - Important Changes (Should Adapt)
...
```

## Step 4: Apply Adaptation Fixes

For each P0/P1 change, apply the fix using the patterns below.

### File Impact Mapping

Map vLLM changes to their vllm-ascend counterparts:

| vLLM Source Path                          | vllm-ascend Target Path                                                          |
| :---------------------------------------- | :------------------------------------------------------------------------------- |
| `vllm/platforms/`                         | `vllm_ascend/platform.py`                                                        |
| `vllm/model_executor/layers/attention/`   | `vllm_ascend/attention/`, `vllm_ascend/ops/mm_encoder_attention.py`              |
| `vllm/model_executor/layers/fused_moe/`   | `vllm_ascend/ops/moe.py`                                                         |
| `vllm/model_executor/layers/layernorm.py` | `vllm_ascend/ops/layernorm.py`                                                   |
| `vllm/model_executor/custom_op.py`        | `vllm_ascend/ops/` (any file registering custom ops)                             |
| `vllm/v1/worker/gpu/model_runner.py`      | `vllm_ascend/worker/model_runner_v1.py`, `vllm_ascend/worker/v2/model_runner.py` |
| `vllm/v1/worker/gpu/spec_decode/`         | `vllm_ascend/spec_decode/`                                                       |
| `vllm/distributed/`                       | `vllm_ascend/distributed/`                                                       |
| `vllm/config*.py`                         | `vllm_ascend/ascend_config.py`                                                   |
| `vllm/compilation/`                       | `vllm_ascend/compilation/` or config overrides                                   |

### Fix Patterns

#### Pattern: Method Signature Change
- **Error:** `TypeError: forward_oot() got an unexpected keyword argument 'X'` or `missing 1 required positional argument: 'X'`
- **Cause:** vLLM changed a method signature — parameter added, removed, renamed, or full API replacement.
- **Fix:** Compare signatures at good vs bad commit, then adapt:
```python
from vllm_ascend.utils import vllm_version_is

# Option 1: Add parameter conditionally to call site
kwargs = {"existing_param": value}
if not vllm_version_is("0.16.0"):  # version before the change
    kwargs["new_param"] = new_value
function(**kwargs)

# Option 2: Add default parameter to OOT method signature
def forward_oot(self, query, key, value, cu_seqlens=None, max_seqlen=None, new_param=None):
```
For full API replacements, adapt the call site to match the new API — do NOT blindly add the old parameter.
**Important:** When creating version-guarded branches, all branches must define the function with identical signatures (convert lambdas to `def` if needed). Mismatched signatures across branches cause mypy `[call-arg]` errors.

#### Pattern: Config/Attribute Change
- **Error:** `AttributeError: 'CompilationConfig' object has no attribute 'X'`, `KeyError: 'field_name'`, or `Config object has no attribute 'Y'`
- **Cause:** Upstream moved an attribute/config field between classes, restructured a config class, or added a new required field.
- **Fix:** Use `vllm_version_is()` to access from the correct location:
```python
if vllm_version_is('0.16.0'):
    value = self.vllm_config.old_location.attribute
else:
    value = self.new_class.new_location.attribute
```
For config access that changes frequently, consider helper methods to abstract the logic. For new required fields, add them to the config wrapper.

#### Pattern: Custom Op Not Registered
- **Error:** `AttributeError: '_OpNamespace' '_C' object has no attribute 'op_name'`
- **Cause:** vLLM code references `torch.ops._C.op_name` — a CUDA custom op not available on Ascend
- **Fix:** Register an equivalent Ascend op, or override config to use a different code path

#### Pattern: Method Return Type Change
- **Error:** `TypeError: '>' not supported between instances of 'NoneType' and 'NoneType'`
- **Cause:** Upstream changed a method from returning `None` to returning a value, and the caller now uses it.
- **Fix:** Update the OOT override to return the expected value.

#### Pattern: Module Reorganization
- **Error:** `ImportError: cannot import name 'X' from 'vllm.old.path'`
- **Cause:** vLLM moved/renamed a module, or removed it entirely.
- **Fix:** For moved/renamed modules, use `vllm_version_is()` to branch imports. For removed modules, delete the import **and** all usages — clean removal over `# type: ignore`.

#### Pattern: Platform Interface Addition
- **Error:** `TypeError: Can't instantiate abstract class AscendPlatform with abstract method X`
- **Cause:** New abstract method added to vLLM's `Platform` base class
- **Fix:** Implement the method in `vllm_ascend/platform.py`

#### Pattern: Environment Flakes (NO FIX NEEDED)
- `OSError: [Errno 116] Stale file handle` — multi-process NFS race
- `ConnectionResetError` — transient network
- `filelock` errors — model download contention
- These should be noted in the report but require no code changes

### Version Compatibility

```python
from vllm_ascend.utils import vllm_version_is

if vllm_version_is("0.15.0"):
    # Old API
else:
    # New API
```

## Step 5: Update Commit Hash References

Replace OLD_COMMIT with NEW_COMMIT across all CI files:
```bash
grep -Frl "${OLD_COMMIT}" .github/ docs/ | xargs sed -i "s/${OLD_COMMIT}/${NEW_COMMIT}/g"
```

Verify:
```bash
grep -Frn "${OLD_COMMIT}" .github/ docs/
# Should return nothing
```

## Step 6: Commit

**CI mode:** Commit to current branch, do NOT create PR (orchestrator handles it):
```bash
git add -u
git commit -m "feat: upgrade to vLLM main (${OLD_COMMIT:0:12}..${NEW_COMMIT:0:12})"
```

**Interactive mode:** Create branch and PR as before.

## Key File Locations

| Project                           | Path                                            |
| --------------------------------- | ----------------------------------------------- |
| vLLM Ascend version compatibility | `docs/source/community/versioning_policy.md`    |
| vLLM Ascend source code           | `vllm_ascend/`                                  |
| **Core Modules**                  |                                                 |
| Ascend-specific attention         | `vllm_ascend/attention/`                        |
| Ascend-specific executor          | `vllm_ascend/worker/`                           |
| Ascend-specific ops               | `vllm_ascend/ops/`                              |
| **Specialized Implementations**   |                                                 |
| Ascend 310P specific              | `vllm_ascend/_310p/`                            |
| EPLB load balancing               | `vllm_ascend/eplb/`                             |
| XLite compiler                    | `vllm_ascend/xlite/`                            |
| **Compilation & Fusion**          |                                                 |
| Graph fusion pass manager         | `vllm_ascend/compilation/`                      |
| Compilation passes                | `vllm_ascend/compilation/passes/`               |
| **Quantization**                  |                                                 |
| Quantization methods              | `vllm_ascend/quantization/`                     |
| ModelSlim integration             | `vllm_ascend/quantization/methods/modelslim/`   |
| **Distributed & KV Cache**        |                                                 |
| KV transfer                       | `vllm_ascend/distributed/kv_transfer/`          |
| Device communicators              | `vllm_ascend/distributed/device_communicators/` |
| **Speculative Decoding**          |                                                 |
| MTP proposer                      | `vllm_ascend/spec_decode/mtp_proposer.py`       |
| Eagle proposer                    | `vllm_ascend/spec_decode/eagle_proposer.py`     |
| **Utility Modules**               |                                                 |
| Common utilities                  | `vllm_ascend/utils.py`                          |
| Ascend config                     | `vllm_ascend/ascend_config.py`                  |
| Platform detection                | `vllm_ascend/platform.py`                       |
| Environment variables             | `vllm_ascend/envs.py`                           |

## Important Notes

1. **Version Checking**: vLLM Ascend uses version checking to maintain compatibility with multiple vLLM versions. Preserve or update related logic when adapting.

2. **Test Verification**: After adaptation, tests must verify:
    - Compatibility with the latest vLLM version
    - Backward compatibility with older vLLM versions
    - Ascend NPU functionality works correctly

3. **Documentation Sync**: If vLLM documentation has significant changes, update vLLM Ascend's documentation accordingly.

4. **Backward Compatibility**:
    - Maintain compatibility from the version currently adapted by vLLM Ascend to the latest version
    - Use version checking to handle code branches for different versions

5. Do not forget to update the vLLM version in `.github` CI files.

6. **Change Logging**: After adaptation, clearly document in the commit message:
   - The range of adapted vLLM commits
   - Main changes made
   - Test results

7. The vLLM python code is under `vllm/vllm` folder.

## Reference

- [Versioning Policy](../../../docs/source/community/versioning_policy.md) - vLLM Ascend versioning strategy
