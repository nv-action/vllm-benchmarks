{{INSTRUCTIONS}}

## Trusted project guidelines

Paths in these guidelines are relative to the trusted checkout at `{{TRUSTED_ROOT}}`.

{{PROJECT_GUIDELINES}}

## Trusted review rules

Knowledge base: `{{TRUSTED_ROOT}}/.github/agent-review`
Touched domains: {{DOMAINS}}

{{KB_MAP}}

## Pull request

Title: {{PR_TITLE}}

{{PR_BODY}}

## Review snapshot

Target branch tip: {{BASE_SHA}}
Merge base: {{MERGE_BASE_SHA}}
PR head: {{HEAD_SHA}}
Upstream vLLM source: `../vllm` at `{{VLLM_SHA}}`.

Your working directory is the PR head. The stat below shows the scope of the
change. Inspect the changes with `git diff --no-ext-diff --no-textconv {{MERGE_BASE_SHA}} HEAD`.
You can add `-- <path>` to focus on a file or use Git history to check earlier behavior.

```text
{{DIFF_STAT}}
```

## Previous findings

{{FINDINGS}}

## Developer's reply

{{REPLY}}

---

Return the JSON object specified in the Review output section of the instructions above.
