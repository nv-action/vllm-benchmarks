"""
CI AI diagnosis agent — single-round text-attachment-package mode.

This script collects all available CI diagnosis sources (main log,
artifacts, benchmark, K8s) into a single `Diagnosis Package` prompt,
sends it to an LLM in one call, and renders the result as structured
JSON + Markdown.  No routing round, no evidence tools, no window
selection — the full package is handed to the model.

Modes:
- Disabled / missing config: emit skip note, exit 0.
- Live LLM call: single-round text-attachment-package diagnosis.
- The agent NEVER breaks CI: every failure path exits 0.

Configuration (env vars, prefixed by VLLM_ASCEND_CI_AI_DIAGNOSIS_*):

    VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED            default 0
    VLLM_ASCEND_CI_AI_DIAGNOSIS_API_KEY            required to call LLM
    VLLM_ASCEND_CI_AI_DIAGNOSIS_BASE_URL           e.g. https://api.openai.com/v1
    VLLM_ASCEND_CI_AI_DIAGNOSIS_MODEL              required for LLM (repo variable)
    VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND            default openai_compatible
    VLLM_ASCEND_CI_AI_DIAGNOSIS_TIMEOUT_S          default 120
    VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_INPUT_CHARS    default 120000
    VLLM_ASCEND_CI_AI_DIAGNOSIS_TAIL_CHARS         default 200000 (tail size for large files)
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as _dt
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regex as re

# Ensure sibling scripts are importable when run as a CLI from the
# workflow scripts directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ci_log_filter_llm import build_llm_log_bundle, clip_text  # noqa: E402

SCHEMA_VERSION = "1.0"
ALLOWED_STAGES = {"setup", "collection", "startup", "runtime", "assertion", "teardown", "infra", "unknown"}
ALLOWED_LAYERS = {
    "ci_workflow",
    "pytest",
    "dependency",
    "vllm_engine",
    "vllm_ascend",
    "npu_runtime",
    "kubernetes",
    "network",
    "storage",
    "external_service",
    "unknown",
}
ALLOWED_CONF = {"high", "medium", "low"}
ALLOWED_CLASS = {"flake", "test_bug", "product_bug", "infra_issue", "unknown"}


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    enabled: bool
    api_key: str
    base_url: str
    model: str
    backend: str
    max_rounds: int
    timeout_s: int
    max_input_chars: int
    tail_chars: int

    @staticmethod
    def _to_bool(value: object) -> bool:
        """Convert an env-var value (str, bool, int, None, …) to bool.

        Accepted true forms: True, 1, '1', 'true', 'yes', 'on' (case-insensitive).
        Anything else is treated as false.
        """
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return False

    @staticmethod
    def _to_int(value: object, default: int) -> int:
        """Convert an env-var value to int, swallowing TypeError / ValueError."""
        if isinstance(value, bool):
            return int(value)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip():
            try:
                return int(value.strip())
            except (TypeError, ValueError):
                pass
        return default

    @classmethod
    def from_env(cls) -> "AgentConfig":
        try:
            import vllm_ascend.envs as envs

            return cls(
                enabled=cls._to_bool(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED),
                api_key=str(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_API_KEY or "").strip(),
                base_url=str(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_BASE_URL or "").strip(),
                model=str(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_MODEL or "").strip(),
                backend=str(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND or "openai_compatible").strip(),
                max_rounds=cls._to_int(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_ROUNDS, 3),
                timeout_s=cls._to_int(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_TIMEOUT_S, 30),
                max_input_chars=cls._to_int(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_INPUT_CHARS, 120000),
                tail_chars=cls._to_int(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_TAIL_CHARS, 200000),
            )
        except (ImportError, AttributeError):
            return cls(
                enabled=cls._to_bool(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED", "0")),
                api_key=os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_API_KEY", "").strip(),
                base_url=os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_BASE_URL", "").strip(),
                model=os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_MODEL", "").strip(),
                backend=os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND", "openai_compatible").strip(),
                max_rounds=cls._to_int(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_ROUNDS", "3"), 3),
                timeout_s=cls._to_int(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_TIMEOUT_S", "30"), 30),
                max_input_chars=cls._to_int(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_INPUT_CHARS", "120000"), 120000),
                tail_chars=cls._to_int(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_TAIL_CHARS", "200000"), 200000),
            )

    def llm_ready(self) -> bool:
        """True when repo configuration is complete enough to call the LLM."""
        return (
            self.enabled
            and bool(self.api_key)
            and bool(self.base_url)
            and bool(self.model)
            and self.backend == "openai_compatible"
        )

    def missing_llm_config(self) -> list[str]:
        """Human-readable list of unset CI LLM settings."""
        missing: list[str] = []
        if not self.enabled:
            missing.append("VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED (must be 1)")
        if not self.api_key:
            missing.append("VLLM_ASCEND_CI_AI_DIAGNOSIS_API_KEY (secret)")
        if not self.base_url:
            missing.append("VLLM_ASCEND_CI_AI_DIAGNOSIS_BASE_URL (secret)")
        if not self.model:
            missing.append("VLLM_ASCEND_CI_AI_DIAGNOSIS_MODEL (variable)")
        if self.backend != "openai_compatible":
            missing.append(f"VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND (unsupported: {self.backend!r})")
        return missing


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a senior CI diagnosis agent for vllm-ascend (Huawei Ascend NPU + vLLM).\n"
    "You receive a two-phase filtered log bundle from this CI run:\n\n"
    "### Phase A — high-signal lines (keyword filtered)\n"
    "Lines containing error|fail|exception|traceback|fatal|timeout|oom|ascend|cann|hccl etc.\n"
    "Each line is prefixed `L<number>:` showing its original line number.\n\n"
    "### Phase B — local context around failure anchors\n"
    "Contiguous regions around FAILURES banners, tracebacks, Error/Exception lines,\n"
    "with 40 lines before and 80 lines after each anchor.\n"
    "Lines also prefixed `L<number>:`.  Multiple regions separated by `--- lines X-Y ---`.\n\n"
    "Additional artifact files (ascend device logs, benchmark results, k8s diagnostics)\n"
    "may follow as plain text attachments.\n\n"
    "TASK:\n"
    "Read ALL provided content.  Find the FIRST non-wrapper root cause.\n"
    "Pytest wrapper errors like 'Server at ... exited unexpectedly', "
    "'Engine core initialization failed', 'some aisbench cases failed', "
    "'benchmark assertion', or 'FAILED tests/...' are SYMPTOMS, never root "
    "causes.  When you see one of them, look nearby in the Phase A/B content for "
    "the upstream error (RuntimeError, AssertionError, CANN, HCCL, ACL, OOM, etc.).\n\n"
    "OUTPUT FORMAT:\n"
    "Reply with a SINGLE fenced JSON block:\n"
    "```json\n"
    "{\n"
    '  "failure_family": "pytest|engine_startup|benchmark|infra|unknown",\n'
    '  "root_cause": "<one sentence describing the first non-wrapper cause>",\n'
    '  "classification": "flake|test_bug|product_bug|infra_issue|unknown",\n'
    '  "confidence": "high|medium|low",\n'
    '  "failure_stage": "setup|collection|startup|runtime|assertion|teardown|infra|unknown",\n'
    '  "failure_layer": "ci_workflow|pytest|dependency|vllm_engine|vllm_ascend|npu_runtime|kubernetes|...",\n'
    '  "visible_failure": "<the outermost error visible in the main log>",\n'
    '  "wrapper_errors": [{"type": "...", "line": N, "snippet": "..."}],\n'
    '  "evidence": [{"file": "...", "line": N, "snippet": "...", "interpretation": "..."}],\n'
    '  "counter_evidence": [{"file": "...", "line": N, "snippet": "...", "interpretation": "..."}],\n'
    '  "failed_tests": ["tests/..."],\n'
    '  "matched_playbooks": [],\n'
    '  "next_actions": [{"priority": "P0|P1|P2", "action": "...", "command": "..."}],\n'
    '  "needs_human_review": true|false\n'
    "}\n"
    "```\n\n"
    "RULES:\n"
    "- root_cause must reference specific L<number> from the filtered log.\n"
    "- If root_cause is unclear, set confidence=low and needs_human_review=true.\n"
    "- DO NOT invent a root cause when evidence is insufficient.\n"
    "- Reply in Chinese for human-facing text. JSON keys must remain English."
)


def _log(stage: str, msg: str) -> None:
    """Stage-tagged progress log. Goes to stderr so it is visible even
    when the main output is a JSON file. Keep it machine-greppable."""
    sys.stderr.write(f"[ci-ai-diagnosis] {stage}: {msg}\n")
    sys.stderr.flush()


def _chat_one(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: list[dict[str, str]],
    timeout_s: int,
    max_input_chars: int,
    stream: bool = True,
) -> str:
    """Single LLM call with progress + sanitised errors.

    When ``stream`` is True, the request uses ``stream: true`` and we report
    time-to-first-byte so the user can distinguish "model is slow" from
    "agent is hung". The full body is still reconstructed in memory and
    returned as a string so downstream parsing is identical to non-stream.
    """
    if not base_url:
        raise RuntimeError("base_url is empty")
    if not api_key:
        raise RuntimeError("api_key is empty")
    url = base_url.rstrip("/") + "/chat/completions"
    body = json.dumps(
        {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
            "stream": bool(stream),
        }
    ).encode("utf-8")
    if len(body) > max_input_chars * 2:
        raise RuntimeError(f"request body too large: {len(body)} chars")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
            "Accept": "text/event-stream" if stream else "application/json",
        },
    )
    started = _dt.datetime.now(_dt.timezone.utc)
    try:
        resp_ctx = urllib.request.urlopen(req, timeout=timeout_s)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code} from {url}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"URLError from {url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise RuntimeError(f"timeout after {timeout_s}s calling {url}") from exc

    try:
        with resp_ctx as resp:
            if stream:
                content_parts: list[str] = []
                first_byte_at: _dt.datetime | None = None
                for raw_line in resp:
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        continue
                    if line.startswith(":"):  # SSE comment
                        continue
                    if not line.startswith("data:"):
                        continue
                    payload_str = line[5:].strip()
                    if payload_str == "[DONE]":
                        break
                    if first_byte_at is None:
                        first_byte_at = _dt.datetime.now(_dt.timezone.utc)
                        elapsed = (first_byte_at - started).total_seconds()
                        _log("stream", f"first byte in {elapsed:.2f}s")
                    try:
                        obj = json.loads(payload_str)
                    except json.JSONDecodeError:
                        continue
                    for choice in obj.get("choices") or []:
                        delta = (choice.get("delta") or {}).get("content")
                        if delta:
                            content_parts.append(delta)
                content = "".join(content_parts)
                if not content:
                    raise RuntimeError("stream returned no content")
                return content

            raw = resp.read().decode("utf-8", errors="replace")
    except OSError:
        raise  # re-raise socket errors from resp.read() / SSE iteration
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"non-JSON response from {url}: {exc}") from exc
    choices = payload.get("choices") or []
    if not choices:
        raise RuntimeError(f"no choices in response: {str(payload)[:2000]}")
    msg = choices[0].get("message") or {}
    content = msg.get("content")
    if not content:
        raise RuntimeError(f"empty message content: {str(payload)[:2000]}")
    return str(content)


_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _strip_text(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    head = cap // 2
    tail = cap - head - 80
    return s[:head] + f"\n... [truncated at {cap} chars] ...\n" + s[-tail:]


def _parse_json_block(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from a markdown-style fenced block.
    Falls back to the first balanced { ... } if no fence is present."""
    m = _JSON_FENCE.search(text)
    candidate = m.group(1) if m else None
    if candidate is None:
        # try to find a balanced JSON object
        start = text.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    candidate = text[start : i + 1]
                    break
    if candidate is None:
        return None
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        return None


def _extract_json_blocks(text: str) -> list[dict[str, Any]]:
    """Extract all valid JSON objects from markdown fenced blocks.

    Uses balanced-bracket matching to correctly handle nested JSON
    (e.g. an array of hypothesis objects inside a HYPOTHESES block),
    unlike the non-greedy regex in ``_parse_json_block``.

    Returns successfully parsed dicts in order of appearance;
    non-dict JSON values are silently dropped.
    """
    blocks: list[dict[str, Any]] = []
    for m in re.finditer(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL):
        for obj_text in _balanced_json_objects(m.group(1)):
            try:
                obj = json.loads(obj_text)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                blocks.append(obj)
    return blocks


def _balanced_json_objects(text: str):
    """Yield balanced { ... } JSON object strings from *text*.
    Handles escaped quotes and nested braces."""
    i = 0
    n = len(text)
    while i < n:
        start = text.find("{", i)
        if start < 0:
            break
        depth = 0
        in_string = False
        escape = False
        for j in range(start, n):
            ch = text[j]
            if escape:
                escape = False
                continue
            if ch == "\\":
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    yield text[start : j + 1]
                    i = j + 1
                    break
        else:
            break  # no closing brace for this object; stop


# ---------------------------------------------------------------------------
# Schema validation / sanitation
# ---------------------------------------------------------------------------


def _ensure_str_list(x: Any) -> list[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(i) for i in x]
    return [str(x)]


def _sanitize_diagnosis(raw: dict[str, Any], routing: dict[str, Any], failed_tests: list[str]) -> dict[str, Any]:
    classification = raw.get("classification") if raw.get("classification") in ALLOWED_CLASS else "unknown"
    confidence = raw.get("confidence") if raw.get("confidence") in ALLOWED_CONF else "low"
    # root_cause may arrive as a dict {type, description} — extract the human part.
    rc = raw.get("root_cause")
    if isinstance(rc, dict):
        rc = str(rc.get("description") or rc.get("hypothesis") or rc.get("detail") or rc.get("summary") or "")
    root_cause = str(rc or "").strip()
    return {
        "routing": routing,
        "failure_family": str(raw.get("failure_family") or "unknown"),
        "root_cause": root_cause,
        "classification": classification,
        "confidence": confidence,
        "evidence": _ensure_evidence_list(raw.get("evidence")),
        "counter_evidence": _ensure_evidence_list(raw.get("counter_evidence")),
        "wrapper_errors": routing.get("wrapper_errors", []),
        "failed_tests": failed_tests,
        "matched_playbooks": _ensure_str_list(raw.get("matched_playbooks")),
        "next_actions": _ensure_actions(raw.get("next_actions")),
        "needs_human_review": bool(raw.get("needs_human_review", True)) or confidence == "low",
    }


def _ensure_evidence_list(x: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(x, list):
        return out
    for e in x:
        if not isinstance(e, dict):
            continue
        out.append(
            {
                "file": str(e.get("file") or ""),
                "line": int(e.get("line") or 0),
                "snippet": str(e.get("snippet") or "")[:240],
                "interpretation": str(e.get("interpretation") or ""),
            }
        )
    return out


def _ensure_actions(x: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(x, list):
        return out
    for a in x:
        if not isinstance(a, dict):
            continue
        pr = a.get("priority") if a.get("priority") in {"P0", "P1", "P2"} else "P1"
        out.append(
            {
                "priority": pr,
                "action": str(a.get("action") or ""),
                "command": str(a.get("command") or ""),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Skip / fallback
# ---------------------------------------------------------------------------


def _skip_diagnosis(reason: str, step_name: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "step_name": step_name,
        "log_file": "",
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "routing": {
            "failure_stage": "unknown",
            "failure_layer": "unknown",
            "visible_failure": reason,
            "first_failure_signal": "",
            "wrapper_errors": [],
            "candidate_routes": [],
            "evidence_requests": [],
            "matched_playbooks": [],
        },
        "failure_family": "unknown",
        "root_cause": "diagnosis skipped",
        "classification": "unknown",
        "confidence": "low",
        "evidence": [],
        "counter_evidence": [],
        "wrapper_errors": [],
        "failed_tests": [],
        "matched_playbooks": [],
        "next_actions": [
            {"priority": "P0", "action": "Investigate manually", "command": ""},
        ],
        "needs_human_review": True,
    }


def _make_evidence_only_diagnosis(
    *,
    step_name: str,
    log_file: Path,
    git_context: dict[str, Any] | None,
    missing_config: list[str],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "evidence_only",
        "step_name": step_name,
        "log_file": str(log_file),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_context": git_context,
        "llm_analysis_performed": False,
        "missing_llm_config": missing_config,
        "routing": {
            "failure_stage": "unknown",
            "failure_layer": "unknown",
            "visible_failure": "",
            "first_failure_signal": "",
            "wrapper_errors": [],
            "candidate_routes": [],
            "evidence_requests": [],
            "matched_playbooks": [],
        },
        "failure_family": "unknown",
        "root_cause": "LLM analysis not performed (CI secrets/variables not configured)",
        "classification": "unknown",
        "confidence": "low",
        "evidence": [],
        "counter_evidence": [],
        "wrapper_errors": [],
        "failed_tests": [],
        "matched_playbooks": [],
        "next_actions": [
            {
                "priority": "P0",
                "action": "Configure CI LLM settings and re-run",
                "command": "",
            },
        ],
        "needs_human_review": True,
    }


def render_evidence_summary(diag: dict[str, Any]) -> str:
    """Markdown for evidence-only mode (no LLM root-cause claim)."""
    missing = diag.get("missing_llm_config") or []
    lines: list[str] = []
    lines.append("## CI AI 失败定位（未调用 LLM）")
    lines.append("")
    lines.append(f"**Step**: {diag.get('step_name') or ''}")
    lines.append(f"**Log**: `{diag.get('log_file') or ''}`")
    lines.append("")
    lines.append("### 未进行 AI 分析的原因")
    if missing:
        for item in missing:
            lines.append(f"- 未配置 `{item}`")
    else:
        lines.append("- LLM 配置不完整")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Single-round diagnosis
# ---------------------------------------------------------------------------

_DEFAULT_TAIL_CHARS = 200_000


def build_diagnosis_package(
    log_file: Path,
    artifact_dir: Path | None = None,
    k8s_dir: Path | None = None,
    benchmark_dir: Path | None = None,
    max_package_chars: int = 500_000,
    tail_chars: int = _DEFAULT_TAIL_CHARS,
) -> str:
    """Build a text-attachment package with two-phase log filtering + artifact files."""
    parts: list[str] = []

    # ---- main log: two-phase filtering (Phase A keywords + Phase B anchor windows) ----
    try:
        raw_log = log_file.read_text(encoding="utf-8", errors="replace")
        filtered_bundle = build_llm_log_bundle(raw_log)
        filtered_bundle = clip_text(filtered_bundle, max_chars=max_package_chars - 5000)
        parts.append(filtered_bundle)
    except (OSError, UnicodeDecodeError):
        parts.append(f"[READ_ERROR] main log: {log_file}")

    # ---- artifact / k8s / benchmark files (tail mode, lower priority) ----
    for d, label_prefix in [(artifact_dir, "artifact"), (k8s_dir, "k8s"), (benchmark_dir, "benchmark")]:
        if d is None or not d.is_dir():
            continue
        for f in sorted(d.rglob("*")):
            if not f.is_file():
                continue
            try:
                content = f.read_text(encoding="utf-8", errors="replace")
            except (OSError, UnicodeDecodeError):
                continue
            if len(content) > tail_chars:
                trunc_note = f"[FILE TRUNCATED: last {tail_chars} / {len(content)} chars]\n\n"
                content = trunc_note + content[-tail_chars:]
            rel = f.relative_to(d)
            parts.append(f"\n--- ATTACHMENT: {label_prefix}-{rel}\n    file: {f}\n---\n{content.rstrip()}\n")

    return "\n".join(parts)


def _build_diagnosis_user(
    step_name: str,
    package: str,
    user_hint: str | None = None,
    git_context: dict[str, Any] | None = None,
) -> str:
    """Build the user prompt with filtered log bundle."""
    parts: list[str] = [
        f"## CI Run: {step_name}\n",
    ]
    if git_context and git_context.get("commit"):
        c = git_context["commit"]
        parts.append(f"\n**Trigger commit**: {c.get('hash', '')[:8]} {c.get('subject', '')}\n")
        changed = git_context.get("changed_files") or []
        if changed:
            parts.append(f"**Changed files ({len(changed)})**: {', '.join(changed[:10])}\n")
    if user_hint:
        parts.append(f"\n**User hint**: {user_hint}\n")
    parts.append("\n---\n\n## Filtered Log Bundle (two-phase extraction)\n\n")
    parts.append(package)
    return "".join(parts)


def run_diagnosis(
    cfg: AgentConfig,
    log_file: Path,
    step_name: str,
    user_hint: str | None = None,
    artifact_dir: Path | None = None,
    git_context: dict[str, Any] | None = None,
    k8s_dir: Path | None = None,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    if not cfg.llm_ready():
        missing = cfg.missing_llm_config()
        _log("skip", f"LLM not configured: {', '.join(missing)}")
        return _skip_diagnosis(
            "LLM analysis not performed (CI secrets/variables not configured: " + ", ".join(missing) + ")",
            step_name,
        )
    if cfg.backend != "openai_compatible":
        _log("skip", f"backend {cfg.backend!r} not supported")
        return _skip_diagnosis(f"backend {cfg.backend!r} not supported", step_name)
    if not log_file.exists() or log_file.stat().st_size == 0:
        _log("skip", f"log file missing or empty: {log_file}")
        return _skip_diagnosis("log file missing or empty", step_name)

    _log("init", f"log={log_file} size={log_file.stat().st_size} model={cfg.model}")

    _log("package", "building filtered log bundle")
    package = build_diagnosis_package(
        log_file,
        artifact_dir=artifact_dir,
        k8s_dir=k8s_dir,
        benchmark_dir=benchmark_dir,
        max_package_chars=cfg.max_input_chars,
        tail_chars=cfg.tail_chars,
    )
    _log("package", f"package chars={len(package)}")

    user_content = _build_diagnosis_user(step_name, package, user_hint, git_context)
    _log("llm", f"sending single-round request (prompt_chars~={len(user_content)})")

    try:
        reply = _chat_one(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            timeout_s=cfg.timeout_s,
            max_input_chars=cfg.max_input_chars,
        )
    except RuntimeError as exc:
        _log("llm", f"failed: {type(exc).__name__}: {exc}")
        return _skip_diagnosis(f"LLM request failed: {type(exc).__name__}: {exc}", step_name)
    _log("llm", f"received reply chars={len(reply)}")

    raw = _parse_json_block(reply) or {}
    routing = _sanitize_single_round_routing(raw)
    diagnosis = _sanitize_diagnosis(raw, routing, failed_tests=raw.get("failed_tests") or [])
    diagnosis.update(
        {
            "schema_version": SCHEMA_VERSION,
            "step_name": step_name,
            "log_file": str(log_file),
            "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        }
    )
    return diagnosis


def _sanitize_single_round_routing(raw: dict[str, Any]) -> dict[str, Any]:
    return {
        "failure_stage": raw.get("failure_stage") if raw.get("failure_stage") in ALLOWED_STAGES else "unknown",
        "failure_layer": raw.get("failure_layer") if raw.get("failure_layer") in ALLOWED_LAYERS else "unknown",
        "visible_failure": str(raw.get("visible_failure") or ""),
        "first_failure_signal": str(raw.get("first_failure_signal") or ""),
        "wrapper_errors": _sanitize_wrapper_errors(raw),
        "candidate_routes": [],
        "evidence_requests": [],
        "matched_playbooks": _ensure_str_list(raw.get("matched_playbooks")),
    }


def _sanitize_wrapper_errors(raw: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for w in raw.get("wrapper_errors") or []:
        if isinstance(w, dict):
            out.append(
                {
                    "type": str(w.get("type") or "Unknown"),
                    "line": int(w.get("line") or 0),
                    "snippet": str(w.get("snippet") or "")[:240],
                }
            )
    return out


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render_markdown(diag: dict[str, Any]) -> str:
    routing = diag.get("routing") or {}
    lines: list[str] = []
    lines.append("## CI AI 失败定位")
    lines.append("")
    lines.append(f"**Step**: {diag.get('step_name') or ''}")
    lines.append(f"**Log**: `{diag.get('log_file') or ''}`")
    lines.append(f"**Stage / Layer**: `{routing.get('failure_stage')}` / `{routing.get('failure_layer')}`")
    lines.append(f"**Classification**: `{diag.get('classification')}`")
    lines.append(f"**Confidence**: `{diag.get('confidence')}`")
    lines.append(f"**Needs human review**: `{diag.get('needs_human_review')}`")
    lines.append("")

    if routing.get("visible_failure"):
        lines.append(f"**Visible failure**: {routing['visible_failure']}")
    if routing.get("first_failure_signal"):
        lines.append(f"**First signal**: {routing['first_failure_signal']}")
    if routing.get("matched_playbooks"):
        lines.append(f"**Matched playbooks**: {', '.join(routing['matched_playbooks'])}")
    lines.append("")

    lines.append("### Root cause")
    lines.append(diag.get("root_cause") or "_(no claim)_")
    lines.append("")

    if diag.get("evidence"):
        lines.append("### Evidence")
        lines.append("| file | line | snippet | interpretation |")
        lines.append("|---|---|---|---|")
        for e in diag["evidence"]:
            lines.append(
                f"| {e.get('file', '')} | {e.get('line', '')} | "
                f"`{e.get('snippet', '')}` | "
                f"{e.get('interpretation', '')} |"
            )
        lines.append("")

    if diag.get("counter_evidence"):
        lines.append("### Counter-evidence")
        lines.append("| file | line | snippet | interpretation |")
        lines.append("|---|---|---|---|")
        for e in diag["counter_evidence"]:
            lines.append(
                f"| {e.get('file', '')} | {e.get('line', '')} | "
                f"`{e.get('snippet', '')}` | "
                f"{e.get('interpretation', '')} |"
            )
        lines.append("")

    if diag.get("next_actions"):
        lines.append("### Next actions")
        lines.append("| priority | action | command |")
        lines.append("|---|---|---|")
        for a in diag["next_actions"]:
            lines.append(f"| {a.get('priority', '')} | {a.get('action', '')} | `{a.get('command', '')}` |")
        lines.append("")

    if routing.get("wrapper_errors"):
        lines.append("### Wrapper errors (symptoms, not root cause)")
        for w in routing["wrapper_errors"]:
            lines.append(f"- L{w.get('line', '')} {w.get('type', '')}: `{w.get('snippet', '')}`")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI entry
# ---------------------------------------------------------------------------


def _append_step_summary(text: str) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        sys.stdout.write(text)
        return
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="CI AI diagnosis agent (vllm-ascend).")
    p.add_argument("--log-file", type=Path, required=True)
    p.add_argument("--step-name", default="tests")
    p.add_argument("--artifact-dir", type=Path, default=None)
    p.add_argument("--k8s-dir", type=Path, default=None)
    p.add_argument("--benchmark-dir", type=Path, default=None)
    p.add_argument("--user-hint", default=None)
    p.add_argument("--ref", default=None)
    p.add_argument("--sha", default=None)
    p.add_argument("--repo-dir", type=Path, default=None)
    p.add_argument("--output-json", type=Path, default=None)
    p.add_argument("--write-summary", action="store_true", help="Write Markdown to GITHUB_STEP_SUMMARY.")
    return p.parse_args(argv)


def _build_git_context(
    repo_dir: Path | None,
    ref: str | None,
    sha: str | None,
) -> dict[str, Any]:
    ctx: dict[str, Any] = {"ref": ref or "", "sha": sha or ""}
    if repo_dir is None or not repo_dir.is_dir():
        return ctx
    try:
        log = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%H%n%an%n%ad%n%s", sha or "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if log.returncode == 0 and log.stdout.strip():
            lines = log.stdout.strip().split("\n")
            ctx["commit"] = {
                "hash": lines[0] if len(lines) > 0 else "",
                "author": lines[1] if len(lines) > 1 else "",
                "date": lines[2] if len(lines) > 2 else "",
                "subject": lines[3] if len(lines) > 3 else "",
            }
    except (subprocess.SubprocessError, OSError):
        pass
    try:
        parent = f"{sha}~1" if sha else "HEAD~1"
        diff = subprocess.run(
            ["git", "-C", str(repo_dir), "diff", "--name-only", parent, sha or "HEAD"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if diff.returncode == 0:
            ctx["changed_files"] = [f for f in diff.stdout.strip().split("\n") if f][:50]
    except (subprocess.SubprocessError, OSError):
        pass
    return ctx


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _main_impl(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = AgentConfig.from_env()
    git_context = _build_git_context(
        repo_dir=args.repo_dir,
        ref=args.ref,
        sha=args.sha,
    )

    if not args.log_file.exists() or args.log_file.stat().st_size == 0:
        _log("skip", f"log file missing or empty: {args.log_file}")
        diag = _skip_diagnosis("log file missing or empty", args.step_name)
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(diag, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.write_summary:
            _append_step_summary(
                render_evidence_summary(
                    {
                        "step_name": args.step_name,
                        "log_file": str(args.log_file),
                        "index": {},
                        "missing_llm_config": ["log file missing or empty"],
                    }
                )
            )
        return 0

    _log("init", f"log={args.log_file} size={args.log_file.stat().st_size}")

    ctx = git_context if any(git_context.values()) else None

    if cfg.llm_ready():
        _log("llm", f"calling model={cfg.model}")
        diag = run_diagnosis(
            cfg,
            log_file=args.log_file,
            step_name=args.step_name,
            user_hint=args.user_hint,
            artifact_dir=args.artifact_dir,
            git_context=ctx,
            k8s_dir=args.k8s_dir,
            benchmark_dir=args.benchmark_dir,
        )
        summary_md = render_markdown(diag)
    else:
        missing = cfg.missing_llm_config()
        _log("llm", f"skipped: {', '.join(missing)}")
        diag = _make_evidence_only_diagnosis(
            step_name=args.step_name,
            log_file=args.log_file,
            git_context=ctx,
            missing_config=missing,
        )
        summary_md = render_evidence_summary(diag)

    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(diag, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if args.write_summary:
        _append_step_summary(summary_md)
    else:
        sys.stdout.write(summary_md)
    return 0


def main(argv: list[str] | None = None) -> int:
    try:
        return _main_impl(argv)
    except Exception as exc:
        import traceback

        tb = traceback.format_exc()
        safe_type = type(exc).__name__
        safe_msg = str(exc)[:500]
        md = (
            "## CI AI 失败定位\n\n"
            "**Agent 自身异常**：诊断脚本内部出错，未产出诊断结论。\n\n"
            f"| 异常类型 | 信息 |\n|---|---|\n"
            f"| `{safe_type}` | `{safe_msg}` |\n\n"
        )
        with contextlib.suppress(Exception):
            _append_step_summary(md)
        sys.stderr.write(f"[ci-ai-diagnosis] fatal: {safe_type}: {safe_msg}\n")
        sys.stderr.write(tb[-4000:])
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
