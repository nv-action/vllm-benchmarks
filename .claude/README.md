# vLLM Ascend skills

This directory contains the skills for vLLM Ascend.

Note: Please copy the skills directory `.agents/skills` to `.claude/skills` if you want to use the skills in this repo with Claude code.

## Table of Contents

- [vLLM Ascend main2main Skill](#vllm-ascend-main2main-skill)
- [vLLM Ascend main2main Error Analysis Skill](#vllm-ascend-main2main-error-analysis-skill)

## vLLM Ascend main2main Skill

Migrate changes from the main vLLM repository to the vLLM Ascend repository, ensuring compatibility and performance optimizations for Ascend NPUs.

### What it does

This skill facilitates the process of:

1. Identifying changes in the main vLLM repository.
2. Applying necessary modifications for Ascend support.
3. Validating the changes in an Ascend environment.
4. Delivering a ready-to-merge commit with optimized code and configurations.

### Quick start

1. Open a conversation with the AI agent inside the vllm-ascend dev container.
2. Invoke the skill (e.g. `/main2main`).
3. The agent follows the playbook and produces a ready-to-merge commit.


## vLLM Ascend main2main Error Analysis Skill

Automates root-cause analysis and fixing of vLLM-Ascend CI failures triggered by upstream vLLM main branch updates.

### What it does

This skill implements a 4-phase pipeline to diagnose and fix CI failures:

1. **Context Acquisition**: Extracts failed test cases and mines error logs to figure out the true root causes (filtering out environment flakes).
2. **Change Analysis**: Traces failures to specific upstream vLLM commits based on code diffs.
3. **Report Generation**: Generates a structured diagnostic report (`vllm_error_analyze.md`).
4. **Automated Fix**: Applies adaptation fixes and submits a PR.

### File layout

| File | Purpose |
| ---- | ------- |
| `SKILL.md` | Skill definition, execution playbook and token budget strategy |
| `scripts/extract_and_analyze.py` | Script to parse GitHub Action logs and generate structured JSON reports |

### Quick start

1. Open a conversation with the AI agent inside the vllm-ascend dev container.
2. Invoke the skill (e.g. `/main2main-error-analysis`).
3. Provide a GitHub Actions URL or run ID related to the CI failures (e.g., schedule test failures).
4. The agent will run the analysis script, trace root causes, provide a report, and push a fix PR.