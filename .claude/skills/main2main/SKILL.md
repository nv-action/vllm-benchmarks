---
name: main2main
description: |
  Guides adaptation of vLLM-Ascend to upstream vLLM main branch changes. Supports two workflows:
  (1) Proactive upgrade: analyze vLLM code diff, generate prioritized change report, adapt vllm-ascend code.
  (2) CI failure diagnosis: when schedule_test_vllm_main CI is red, automatically extract errors from logs,
  trace root causes to upstream commits, generate diagnostic report, and apply fixes.

  The skill produces code changes, a report file, and a structured summary. It does NOT perform
  git/PR operations. After the skill completes in standalone mode, create a branch, commit, and
  submit a PR using the structured summary as PR body.

  Use this skill whenever:
  - The user wants to upgrade/adapt vllm-ascend to a newer vLLM commit
  - The user shares a GitHub Actions URL or run ID from schedule_test_vllm_main
  - The user mentions CI failures related to vLLM main branch updates or "main2main" test failures
  - The user says "the nightly CI is red", "schedule tests are failing", or similar
  - The user wants to compare vLLM changes and assess impact on vllm-ascend
  - The user asks to analyze, debug, or fix failures caused by upstream vLLM changes
---

# main2main

Adapt vLLM-Ascend to upstream vLLM main branch evolution — proactively or reactively.

## Scenario Detection

Determine which workflow the user needs, then Read the corresponding document:

**Proactive Upgrade** — Read `proactive-upgrade.md` (in the same directory as this SKILL.md)
- User wants to analyze what changed in vLLM and adapt vllm-ascend
- User mentions upgrading, bumping, or syncing to a newer vLLM commit
- No CI failure is involved; the goal is forward-looking analysis

**CI Failure Diagnosis** — Read `error-analysis.md` (in the same directory as this SKILL.md)
- User shares a GitHub Actions URL, run ID, or mentions CI is red
- User mentions schedule_test_vllm_main failures or "main2main" test failures
- The goal is to diagnose and fix existing breakage

**If both signals are present** (e.g., user says "upstream changed an API and CI is failing"), prefer CI Failure Diagnosis — fixing active breakage takes priority over proactive analysis.

Both workflows share the common knowledge below. After reading the relevant document, also read `reference/error-patterns.md` for concrete fix examples — do this immediately if the user's message already mentions a specific error type (TypeError, AttributeError, ImportError, etc.), or whenever you encounter such errors during analysis.

---

## Common Knowledge

### Version Compatibility Pattern

Most fixes require `vllm_version_is()` guards to maintain backward compatibility:

```python
from vllm_ascend.utils import vllm_version_is

if vllm_version_is("0.16.0"):  # pinned release version
    # Use old API
else:
    # Use new API (main branch)
```

The compatible release version comes from `vllm_version` matrix in `.github/workflows/pr_test_full.yaml`.

### vLLM-to-vllm-ascend File Mapping

| vLLM Source Path | vllm-ascend Target Path |
|:---|:---|
| `vllm/platforms/` | `vllm_ascend/platform.py` |
| `vllm/model_executor/layers/attention/` | `vllm_ascend/attention/`, `vllm_ascend/ops/mm_encoder_attention.py` |
| `vllm/model_executor/layers/fused_moe/` | `vllm_ascend/ops/moe.py` |
| `vllm/model_executor/layers/layernorm.py` | `vllm_ascend/ops/layernorm.py` |
| `vllm/model_executor/custom_op.py` | `vllm_ascend/ops/` (any file registering custom ops) |
| `vllm/v1/worker/gpu/model_runner.py` | `vllm_ascend/worker/model_runner_v1.py`, `vllm_ascend/worker/v2/model_runner.py` |
| `vllm/v1/worker/gpu/spec_decode/` | `vllm_ascend/spec_decode/` |
| `vllm/distributed/` | `vllm_ascend/distributed/` |
| `vllm/config*.py` | `vllm_ascend/ascend_config.py` |
| `vllm/compilation/` | `vllm_ascend/compilation/` |

### Key Areas to Focus On

When analyzing upstream changes, these areas most frequently require vllm-ascend adaptation:

1. **Platform Interface** (`vllm/platforms/`) — new abstract methods, signature changes
2. **MoE** (`vllm/model_executor/layers/fused_moe/`) — FusedMoE layer, activation, router changes
3. **Attention** (`vllm/model_executor/layers/attention/`) — backend changes, MLA updates
4. **Speculative Decoding** (`vllm/v1/worker/gpu/spec_decode/`, `vllm/config/speculative.py`)
5. **Distributed** (`vllm/distributed/`) — parallel state, KV transfer, device communicators
6. **Models** (`vllm/model_executor/models/`) — new architectures, interface changes
7. **Worker/Model Runner** (`vllm/v1/worker/gpu/model_runner.py`)
8. **Quantization** (`vllm/model_executor/layers/quantization/`)

### Key File Locations in vllm-ascend

| Category | Path |
|:---|:---|
| Version compatibility | `docs/source/community/versioning_policy.md` |
| Source code | `vllm_ascend/` |
| Attention | `vllm_ascend/attention/` |
| Worker/Executor | `vllm_ascend/worker/` |
| Custom ops | `vllm_ascend/ops/` |
| 310P specific | `vllm_ascend/_310p/` |
| EPLB load balancing | `vllm_ascend/eplb/` |
| Compilation/Fusion | `vllm_ascend/compilation/` |
| Quantization | `vllm_ascend/quantization/` |
| Distributed/KV Cache | `vllm_ascend/distributed/` |
| Speculative decoding | `vllm_ascend/spec_decode/` |
| Utilities | `vllm_ascend/utils.py` |
| Platform detection | `vllm_ascend/platform.py` |
| Ascend config | `vllm_ascend/ascend_config.py` |
| Environment variables | `vllm_ascend/envs.py` |
| CI workflows | `.github/` |

### Important Notes

1. **Backward Compatibility**: maintain compatibility from the pinned release version to the latest main. Use `vllm_version_is()` for version-specific branches.
2. **vLLM source code** is under the `vllm/vllm/` folder in the vLLM repo.
3. **CI references**: after adapting code, update vLLM commit references in `.github` CI files.
4. **Documentation**: if vLLM docs change significantly, update vllm-ascend docs accordingly.

---

## Output Contract

Both workflows produce three outputs:

1. **Code changes** — applied to the working tree (unstaged)
2. **Report file** — `vllm_changes.md` (proactive upgrade) or `vllm_error_analyze.md` (CI fix), written to the repo root
3. **Structured summary** — output in conversation, following the format defined in each workflow's final step

The skill does **not** perform git or GitHub operations (no branch, commit, push, or PR). After the skill completes:

- **Standalone mode**: proceed with creating a branch, committing changes, pushing, and submitting a PR. Use the structured summary as the PR body content.
- **Workflow mode**: the orchestrating Workflow handles all git/PR operations using the structured summary.
