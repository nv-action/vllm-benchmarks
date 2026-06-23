"""
CI AI diagnosis agent (see ``.agents/skills/ci-ai-diagnosis-agent/SKILL.md``).

This is the top-level entry that:

1. Builds the deterministic log index via ``ci_diagnosis_index.py``.
2. Drives a bounded LLM agent loop with the evidence API from
   ``ci_diagnosis_evidence.py``.
3. Optionally reuses ``log-diagnosis-{problem-id}`` playbooks as
   accelerators, never as authorities.
4. Emits a structured diagnosis JSON (source of truth) and renders a
   Markdown view (human friendly).

Modes:
- Disabled / missing config: emit skip note, exit 0.
- Live LLM call: drive rounds 1-3 per the skill's protocol.
- The agent NEVER breaks CI: every failure path exits 0 and writes a
  skip note to GITHUB_STEP_SUMMARY (when set) and/or stdout.

Configuration (env vars, prefixed by VLLM_ASCEND_CI_AI_DIAGNOSIS_*):

    VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED       default 0
    VLLM_ASCEND_CI_AI_DIAGNOSIS_API_KEY       required to call LLM
    VLLM_ASCEND_CI_AI_DIAGNOSIS_BASE_URL      e.g. https://api.openai.com/v1
    VLLM_ASCEND_CI_AI_DIAGNOSIS_MODEL         required for LLM (repo variable)
    VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND       default openai_compatible
    VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_ROUNDS    default 3
    VLLM_ASCEND_CI_AI_DIAGNOSIS_TIMEOUT_S     default 120
    VLLM_ASCEND_CI_AI_DIAGNOSIS_MAX_INPUT_CHARS default 120000
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

from ci_diagnosis_evidence import (  # noqa: E402
    get_artifact_manifest,
    get_artifact_window,
    get_benchmark_summary,
    get_failure_block,
    get_first_exception_context,
    get_k8s_summary,
    get_last_exception_context,
    get_window,
    get_wrapper_upstream_context,
    list_artifacts,
    search,
    search_artifacts,
)
from ci_diagnosis_index import build_index  # noqa: E402


SCHEMA_VERSION = "1.0"
ALLOWED_STAGES = {"setup", "collection", "startup", "runtime", "assertion", "teardown", "infra", "unknown"}
ALLOWED_LAYERS = {
    "ci_workflow", "pytest", "dependency", "vllm_engine", "vllm_ascend",
    "npu_runtime", "kubernetes", "network", "storage", "external_service", "unknown",
}
ALLOWED_CONF = {"high", "medium", "low"}
ALLOWED_CLASS = {"flake", "test_bug", "product_bug", "infra_issue", "unknown"}

# Cap on evidence requests to keep the agent loop bounded. Must be high
# enough to accommodate all forced evidence (failure block, first/last
# traceback, wrapper upstream, artifact manifest + search, benchmark,
# k8s) plus any the LLM adds.
MAX_EVIDENCE_REQUESTS = 20

# Strip caps for the LLM prompt. Larger values keep more context visible
# but increase token cost.
ROUND1_INDEX_CHARS = 80_000
ROUND2_EVIDENCE_CHARS = 80_000

# Playbooks the agent is allowed to reference. It is an *open* hint set,
# not a closed list: the agent can also report `matched_playbooks = []`
# and fall back to the generic hypothesis protocol.
KNOWN_PLAYBOOKS: tuple[str, ...] = (
    "log-diagnosis-vllm-inference-timeout",
    "log-diagnosis-pd-link-establishment",
    "log-diagnosis-mindie/log-diagnosis-large-ep-startup",
    "log-diagnosis-mindie/log-diagnosis-shrink-p-reserve-d",
    "log-diagnosis-mindie/log-diagnosis-controller-recovery-terminate",
    "log-diagnosis-vllm",
    "log-diagnosis-mindie",
)


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
            missing.append(
                f"VLLM_ASCEND_CI_AI_DIAGNOSIS_BACKEND (unsupported: {self.backend!r})"
            )
        return missing


# ---------------------------------------------------------------------------
# Evidence dispatcher (round 2/3 consume this)
# ---------------------------------------------------------------------------

@dataclass
class EvidenceCollector:
    log_file: Path
    artifact_dir: Path | None = None      # general / ascend-logs / backward compat
    k8s_dir: Path | None = None           # K8s diagnostics (pods.json, events)
    benchmark_dir: Path | None = None     # benchmark_results JSON
    collected: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.collected is None:
            self.collected = []

    def _resolve_k8s(self) -> Path | None:
        return self.k8s_dir or self.artifact_dir

    def _resolve_benchmark(self) -> Path | None:
        return self.benchmark_dir or self.artifact_dir

    def _search_extra_dirs(self) -> list[Path] | None:
        dirs: list[Path] = []
        for d in (self.k8s_dir, self.benchmark_dir):
            if d is not None and d.is_dir():
                dirs.append(d)
        return dirs or None

    def fetch(self, request: dict[str, Any]) -> dict[str, Any]:
        tool = request.get("tool", "")
        try:
            if tool == "get_window":
                payload = get_window(
                    self.log_file,
                    int(request["line"]),
                    before=int(request.get("before", 40)),
                    after=int(request.get("after", 80)),
                )
            elif tool == "search":
                payload = search(self.log_file, str(request["pattern"]), int(request.get("max_matches", 30)))
            elif tool == "get_failure_block":
                payload = get_failure_block(
                    self.log_file,
                    str(request["test"]),
                    before=int(request.get("before", 5)),
                    after=int(request.get("after", 200)),
                )
            elif tool == "get_first_exception_context":
                payload = get_first_exception_context(
                    self.log_file,
                    before=int(request.get("before", 40)),
                    after=int(request.get("after", 80)),
                )
            elif tool == "get_last_exception_context":
                payload = get_last_exception_context(
                    self.log_file,
                    before=int(request.get("before", 40)),
                    after=int(request.get("after", 80)),
                )
            elif tool == "get_wrapper_upstream_context":
                payload = get_wrapper_upstream_context(
                    self.log_file,
                    before=int(request.get("before", 200)),
                    after=int(request.get("after", 40)),
                )
            elif tool == "list_artifacts":
                if self.artifact_dir is None:
                    payload = {"artifact_dir": "", "files": []}
                else:
                    payload = list_artifacts(self.artifact_dir)
            elif tool == "get_artifact_window":
                artifact = Path(self.artifact_dir or "") / str(request["artifact"])
                payload = get_artifact_window(
                    artifact,
                    int(request["line"]),
                    before=int(request.get("before", 40)),
                    after=int(request.get("after", 80)),
                )
            elif tool == "get_artifact_manifest":
                if self.artifact_dir is None:
                    payload = {"error": "artifact_dir not set"}
                else:
                    payload = get_artifact_manifest(
                        self.artifact_dir,
                        k8s_dir=self.k8s_dir,
                        benchmark_dir=self.benchmark_dir,
                    )
            elif tool == "get_benchmark_summary":
                source = self._resolve_benchmark()
                if source is None:
                    payload = {"error": "benchmark_dir not set"}
                else:
                    payload = get_benchmark_summary(source, benchmark_dir=self.benchmark_dir)
            elif tool == "get_k8s_summary":
                source = self._resolve_k8s()
                if source is None:
                    payload = {"error": "k8s_dir not set"}
                else:
                    payload = get_k8s_summary(source, k8s_dir=self.k8s_dir)
            elif tool == "search_artifacts":
                if self.artifact_dir is None:
                    payload = {"error": "artifact_dir not set"}
                else:
                    payload = search_artifacts(
                        self.artifact_dir,
                        str(request["pattern"]),
                        max_matches=int(request.get("max_matches", 30)),
                        extra_dirs=self._search_extra_dirs(),
                    )
            else:
                payload = {"error": f"unknown tool: {tool!r}"}
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as exc:
            payload = {"error": f"{type(exc).__name__}: {exc}"}

        entry = {"request": request, "payload": payload}
        self.collected.append(entry)
        return entry


# ---------------------------------------------------------------------------
# LLM backend
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a senior CI diagnosis agent for vllm-ascend (Huawei Ascend NPU + vLLM).\n"
    "You have access to a structured log index and an evidence API. You do NOT receive a "
    "pre-trimmed log bundle; you decide what to look at.\n\n"
    "You must follow the CI AI Diagnosis Agent protocol:\n"
    "  - Round 1: produce a routing JSON, NOT a root cause.\n"
    "  - Round 2: take the evidence results and produce 2-3 candidate hypotheses, each "
    "    with supporting evidence, counter-evidence, confidence, and next checks. Then "
    "    a FINAL diagnosis JSON with root_cause + classification + confidence.\n\n"
    "AVAILABLE EVIDENCE TOOLS (use these via evidence_requests in round 1):\n"
    "  - {tool:'get_window', line:N, before:M, after:K}  -> context around a line\n"
    "  - {tool:'search', pattern:'REGEX', max_matches:N}  -> all matches with line numbers\n"
    "  - {tool:'get_failure_block', test:'tests/...::test_...', before:M, after:K}  -> pytest FAILURES block\n"
    "  - {tool:'get_first_exception_context', before:M, after:K}  -> window around first Traceback\n"
    "  - {tool:'get_last_exception_context', before:M, after:K}  -> window around last Traceback\n"
    "  - {tool:'get_wrapper_upstream_context', before:M, after:K}  -> trace each wrapper hit upstream\n"
    "  - {tool:'list_artifacts'}  -> list artifact files when artifact_dir is set\n"
    "  - {tool:'get_artifact_window', artifact:'rel/path', line:N, before:M, after:K}\n"
    "  - {tool:'get_artifact_manifest'}  -> typed manifest of artifact bundle "
    "(ascend_logs, k8s, benchmark_results)\n"
    "  - {tool:'search_artifacts', pattern:'REGEX'}  -> regex search across artifact "
    "files (plog, k8s events, pod stdout)\n"
    "  - {tool:'get_benchmark_summary'}  -> pass/fail summary of benchmark_results JSON\n"
    "  - {tool:'get_k8s_summary'}  -> K8s pod state, container states, abnormal reasons\n\n"
    "MINIMUM EVIDENCE REQUESTS (always include in round 1 when applicable):\n"
    "  - If the index has failed_tests, include get_failure_block for the first test.\n"
    "  - If the index has first_traceback, include get_first_exception_context.\n"
    "  - If the index has last_traceback AND it differs from first_traceback, "
    "include get_last_exception_context.\n"
    "  - If the index has wrapper_hits, include get_wrapper_upstream_context.\n"
    "  - If the artifact_manifest has ascend_logs.present, include "
    "get_artifact_manifest and search_artifacts with a broad error pattern.\n"
    "  - If the artifact_summary.benchmark has tasks_failed > 0, include "
    "get_benchmark_summary.\n"
    "  - If the artifact_summary.k8s has abnormal_reasons or non-running "
    "containers, include get_k8s_summary.\n"
    "  - If the failure_layer hints at a domain (NPU / network / storage), add a\n"
    "    search for the relevant tokens (CANN|HCCL|ACL|timeout|400|503|OOM|image pull).\n\n"
    "WRAPPER ERRORS ARE SYMPTOMS, never root causes, unless you have direct log\n"
    "evidence proving the opposite. Common wrappers in vllm-ascend CI:\n"
    "  EngineDeadError, EngineCore encountered a fatal error, subprocess.CalledProcessError,\n"
    "  TimeoutError, 500 Internal Server Error, CrashLoopBackOff, ImagePullBackOff, OOMKilled,\n"
    "  AISBenchRuntimeError, FileMatchError, TINFER-RUNTIME-001, SUMM-FILE-001,\n"
    "  'some aisbench cases failed', pytest AssertionError.\n"
    "For each wrapper, the routing must record it and the round 2 hypothesis must\n"
    "trace it back to the first non-wrapper cause with {file, line} references.\n\n"
    "EVIDENCE RULES:\n"
    "  - Every claim in root_cause and evidence must reference {file, line}.\n"
    "  - supporting_evidence and counter_evidence must each be non-empty for any\n"
    "    hypothesis you do not reject.\n"
    "  - If you cannot justify a candidate, return matched_playbooks=[] and\n"
    "    failure_stage='unknown'. Do not invent.\n"
    "  - classification must be one of: flake, test_bug, product_bug, infra_issue, unknown.\n"
    "  - confidence must be one of: high, medium, low. If low, set needs_human_review=true.\n\n"
    "Reply in Chinese for human-facing text. JSON keys must remain English."
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


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json_blocks(text: str) -> list[str]:
    """Extract outermost JSON objects from markdown fenced blocks.

    Uses bracket counting (not regex) to correctly handle nested objects,
    arrays, and escaped characters inside strings.  Returns the raw text
    of each top-level ``{...}`` found inside a `` ```json `` (or `` ``` ``) fence.
    """
    blocks: list[str] = []
    for m in re.finditer(r"```(?:json)?[ \t]*\n?", text):
        pos = m.end()
        depth = 0
        in_str = False
        escape = False
        block_start = -1
        while pos < len(text):
            ch = text[pos]
            if escape:
                escape = False
                pos += 1
                continue
            if ch == "\\" and in_str:
                escape = True
                pos += 1
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str:
                if ch == "{":
                    if depth == 0:
                        block_start = pos
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0 and block_start >= 0:
                        blocks.append(text[block_start : pos + 1])
                        break
            pos += 1
    return blocks


def _strip_text(s: str, cap: int) -> str:
    if len(s) <= cap:
        return s
    head = cap // 2
    tail = cap - head - 80
    return s[:head] + f"\n... [truncated at {cap} chars] ...\n" + s[-tail:]


def _build_round1_user(index: dict[str, Any], step_name: str, user_hint: str | None) -> str:
    index_text = json.dumps(index, ensure_ascii=False, indent=2)
    hint = f"\nUser hint: {user_hint}\n" if user_hint else ""
    return (
        f"### Step\n{step_name}\n"
        f"### Deterministic log index (built by ci_diagnosis_index.py)\n"
        f"```json\n{_strip_text(index_text, ROUND1_INDEX_CHARS)}\n```\n"
        f"{hint}\n"
        f"Produce a routing JSON. Do NOT pick a root cause yet.\n"
        f"Schema:\n"
        f"  failure_stage (one of setup/collection/startup/runtime/assertion/teardown/infra/unknown)\n"
        f"  failure_layer (one of ci_workflow/pytest/dependency/vllm_engine/vllm_ascend/npu_runtime/kubernetes/network/storage/external_service/unknown)\n"
        f"  visible_failure, first_failure_signal, wrapper_errors (list of {{type,line,snippet}})\n"
        f"  candidate_routes (list of {{route,playbook?,confidence,supporting_evidence,missing_evidence}})\n"
        f"  evidence_requests (list of {{tool,line?,before?,after?,pattern?}})\n"
        f"  matched_playbooks (list of strings; only from KNOWN_PLAYBOOKS or empty)\n"
        f"Reply with a fenced JSON block only."
    )


def _build_round2_user(
    index: dict[str, Any],
    routing: dict[str, Any],
    evidence_collected: list[dict[str, Any]],
) -> str:
    routed = json.dumps(routing, ensure_ascii=False, indent=2)
    evs = json.dumps(evidence_collected, ensure_ascii=False, indent=2)
    return (
        f"### Routing decision (round 1)\n```json\n{_strip_text(routed, 20000)}\n```\n"
        f"### Evidence collected (round 2)\n```json\n{_strip_text(evs, ROUND2_EVIDENCE_CHARS)}\n```\n"
        f"### Index summary\n"
        f"failed_tests: {index.get('failed_tests')}\n"
        f"wrapper_hits: {(index.get('wrapper_hits') or [])[:8]}\n\n"
        f"Produce 2-3 hypotheses, each with:\n"
        f"  hypothesis (one sentence)\n"
        f"  supporting_evidence (list of {{file,line,snippet,interpretation}})\n"
        f"  counter_evidence (list of {{file,line,snippet,interpretation}})\n"
        f"  confidence (high|medium|low)\n"
        f"  next_checks (list of strings)\n\n"
        f"Then propose the final arbitration JSON:\n"
        f"  failure_family, root_cause, classification, confidence, evidence, counter_evidence, "
        f"wrapper_errors, failed_tests, matched_playbooks, next_actions, needs_human_review.\n"
        f"Reply with two fenced JSON blocks: HYPOTHESES and FINAL."
    )


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


# ---------------------------------------------------------------------------
# Schema validation / sanitation
# ---------------------------------------------------------------------------

def _ensure_str_list(x: Any) -> list[str]:
    if x is None:
        return []
    if isinstance(x, list):
        return [str(i) for i in x]
    return [str(x)]


def _sanitize_routing(raw: dict[str, Any]) -> dict[str, Any]:
    routing = {
        "failure_stage": raw.get("failure_stage") if raw.get("failure_stage") in ALLOWED_STAGES else "unknown",
        "failure_layer": raw.get("failure_layer") if raw.get("failure_layer") in ALLOWED_LAYERS else "unknown",
        "visible_failure": str(raw.get("visible_failure") or ""),
        "first_failure_signal": str(raw.get("first_failure_signal") or ""),
        "wrapper_errors": [],
        "candidate_routes": [],
        "evidence_requests": [],
        "matched_playbooks": [],
    }
    for w in raw.get("wrapper_errors") or []:
        if isinstance(w, dict):
            routing["wrapper_errors"].append({
                "type": str(w.get("type") or "Unknown"),
                "line": int(w.get("line") or 0),
                "snippet": str(w.get("snippet") or "")[:240],
            })
    for c in raw.get("candidate_routes") or []:
        if isinstance(c, dict):
            conf = c.get("confidence") if c.get("confidence") in ALLOWED_CONF else "low"
            routing["candidate_routes"].append({
                "route": str(c.get("route") or "unknown"),
                "playbook": str(c.get("playbook") or "") or None,
                "confidence": conf,
                "supporting_evidence": _ensure_str_list(c.get("supporting_evidence")),
                "missing_evidence": _ensure_str_list(c.get("missing_evidence")),
            })
    for er in raw.get("evidence_requests") or []:
        if isinstance(er, dict) and er.get("tool"):
            clean = {k: v for k, v in er.items() if v is not None}
            routing["evidence_requests"].append(clean)
    pbks = raw.get("matched_playbooks") or []
    if isinstance(pbks, list):
        routing["matched_playbooks"] = [str(p) for p in pbks if isinstance(p, str)]
    return routing


def _sanitize_diagnosis(raw: dict[str, Any], routing: dict[str, Any], failed_tests: list[str]) -> dict[str, Any]:
    classification = raw.get("classification") if raw.get("classification") in ALLOWED_CLASS else "unknown"
    confidence = raw.get("confidence") if raw.get("confidence") in ALLOWED_CONF else "low"
    # Normalise root_cause: LLM sometimes returns a dict {type, description}
    # instead of a plain string.
    root_cause_raw = raw.get("root_cause") or ""
    if isinstance(root_cause_raw, dict):
        root_cause_raw = root_cause_raw.get("description") or root_cause_raw.get("hypothesis") or str(root_cause_raw)
    root_cause = str(root_cause_raw).strip()
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
        out.append({
            "file": str(e.get("file") or ""),
            "line": int(e.get("line") or 0),
            "snippet": str(e.get("snippet") or "")[:240],
            "interpretation": str(e.get("interpretation") or ""),
        })
    return out


def _ensure_actions(x: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if not isinstance(x, list):
        return out
    for a in x:
        if not isinstance(a, dict):
            continue
        pr = a.get("priority") if a.get("priority") in {"P0", "P1", "P2"} else "P1"
        out.append({
            "priority": pr,
            "action": str(a.get("action") or ""),
            "command": str(a.get("command") or ""),
        })
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


def _collect_baseline_evidence(
    index: dict[str, Any],
    collector: EvidenceCollector,
) -> None:
    """Deterministic evidence pass (no LLM): same minimum set as routing would request."""
    for req in _enforce_min_evidence_requests(index, []):
        collector.fetch(req)


def _make_evidence_only_diagnosis(
    *,
    step_name: str,
    log_file: Path,
    index: dict[str, Any],
    collector: EvidenceCollector,
    git_context: dict[str, Any] | None,
    missing_config: list[str],
) -> dict[str, Any]:
    """Evidence-only output when LLM credentials are not configured."""
    wrapper_hits = index.get("wrapper_hits") or []
    return {
        "schema_version": SCHEMA_VERSION,
        "type": "evidence_only",
        "step_name": step_name,
        "log_file": str(log_file),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_context": git_context,
        "index": index,
        "collected_evidence": collector.collected,
        "llm_analysis_performed": False,
        "missing_llm_config": missing_config,
        "routing": {
            "failure_stage": "unknown",
            "failure_layer": "unknown",
            "visible_failure": "",
            "first_failure_signal": "",
            "wrapper_errors": wrapper_hits[:20],
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
        "wrapper_errors": wrapper_hits[:20],
        "failed_tests": index.get("failed_tests") or [],
        "matched_playbooks": [],
        "next_actions": [
            {
                "priority": "P0",
                "action": "Configure CI LLM settings and re-run, or investigate using evidence below",
                "command": "",
            },
        ],
        "needs_human_review": True,
    }


def render_evidence_summary(diag: dict[str, Any]) -> str:
    """Markdown for evidence-only mode (no LLM root-cause claim)."""
    index = diag.get("index") or {}
    missing = diag.get("missing_llm_config") or []
    lines: list[str] = []
    lines.append("## CI AI 失败定位（仅证据，未调用 LLM）")
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
    lines.append("### 日志索引摘要")
    lines.append(f"- **Total lines**: {index.get('total_lines')}")
    lines.append(f"- **Failed tests**: {len(index.get('failed_tests') or [])}")
    for test in (index.get("failed_tests") or [])[:5]:
        lines.append(f"  - `{test}`")
    lines.append(f"- **Wrapper hits**: {len(index.get('wrapper_hits') or [])}")
    for w in (index.get("wrapper_hits") or [])[:8]:
        lines.append(f"  - L{w.get('line')} `{w.get('type')}`: `{w.get('snippet', '')[:120]}`")
    if index.get("first_traceback"):
        lines.append(f"- **First traceback**: L{index['first_traceback'].get('start')}")
    lines.append("")
    lines.append("### 已收集证据")
    collected = diag.get("collected_evidence") or []
    if not collected:
        lines.append("_(no evidence collected)_")
    else:
        lines.append("| tool | summary |")
        lines.append("|---|---|")
        for entry in collected[:12]:
            req = entry.get("request") or {}
            payload = entry.get("payload") or {}
            tool = req.get("tool", "?")
            if payload.get("error"):
                summary = f"error: {payload['error']}"
            elif payload.get("matches"):
                summary = f"{len(payload['matches'])} match(es)"
            elif payload.get("start") and payload.get("end"):
                summary = f"L{payload['start']}-L{payload['end']}"
            elif payload.get("text"):
                summary = str(payload.get("text", ""))[:160].replace("\n", " ")
            else:
                summary = json.dumps(payload, ensure_ascii=False)[:160]
            lines.append(f"| `{tool}` | {summary} |")
    lines.append("")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Git context
# ---------------------------------------------------------------------------

def _build_git_context(
    repo_dir: Path | None,
    ref: str | None,
    sha: str | None,
) -> dict[str, Any]:
    """Extract git context so the LLM can correlate failures with code changes."""
    ctx: dict[str, Any] = {"ref": ref or "", "sha": sha or ""}
    if repo_dir is None or not repo_dir.is_dir():
        return ctx
    try:
        log = subprocess.run(
            ["git", "-C", str(repo_dir), "log", "-1", "--format=%H%n%an%n%ad%n%s", sha or "HEAD"],
            capture_output=True, text=True, timeout=10,
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
            capture_output=True, text=True, timeout=10,
        )
        if diff.returncode == 0:
            ctx["changed_files"] = [f for f in diff.stdout.strip().split("\n") if f][:50]
    except (subprocess.SubprocessError, OSError):
        pass
    return ctx

# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------

# Minimum evidence requests the agent must always make, to avoid a common
# failure mode: model looks only at the last 20 lines and declares "failed
# at pytest assertion" without ever reading the test's failure block or
# the first traceback.
def _enforce_min_evidence_requests(index: dict[str, Any], requests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    have: set[str] = set()
    for r in requests:
        if isinstance(r, dict) and r.get("tool"):
            have.add(r["tool"])
    out = list(requests)
    failed = index.get("failed_tests") or []
    if failed and "get_failure_block" not in have:
        out.insert(0, {"tool": "get_failure_block", "test": failed[0], "before": 5, "after": 200})
        have.add("get_failure_block")
    if index.get("first_traceback") and "get_first_exception_context" not in have:
        out.insert(0, {"tool": "get_first_exception_context", "before": 40, "after": 120})
        have.add("get_first_exception_context")
    if (
        index.get("last_traceback")
        and index.get("last_traceback") != index.get("first_traceback")
        and "get_last_exception_context" not in have
    ):
        out.insert(0, {"tool": "get_last_exception_context", "before": 40, "after": 120})
        have.add("get_last_exception_context")
    if (index.get("wrapper_hits") or []) and "get_wrapper_upstream_context" not in have:
        out.insert(0, {"tool": "get_wrapper_upstream_context", "before": 200, "after": 40})
        have.add("get_wrapper_upstream_context")
    return _enforce_min_evidence_requests_artifact(index, out, have)


def _enforce_min_evidence_requests_artifact(
    index: dict[str, Any],
    requests: list[dict[str, Any]],
    have: set[str],
) -> list[dict[str, Any]]:
    summary = index.get("artifact_summary") or {}
    manifest = index.get("artifact_manifest") or {}
    out = list(requests)

    bench = summary.get("benchmark") or {}
    if bench.get("present") and bench.get("tasks_failed", 0) > 0:
        if "get_benchmark_summary" not in have:
            out.insert(0, {"tool": "get_benchmark_summary"})
            have.add("get_benchmark_summary")

    k8s = summary.get("k8s") or {}
    if k8s.get("pods_json_present"):
        abnormal = k8s.get("abnormal_reasons") or []
        non_running = (k8s.get("container_states") or {}).get("waiting", 0)
        non_running += (k8s.get("container_states") or {}).get("terminated", 0)
        if (abnormal or non_running > 0) and "get_k8s_summary" not in have:
            out.insert(0, {"tool": "get_k8s_summary"})
            have.add("get_k8s_summary")

    ascend_present = (manifest.get("ascend_logs") or {}).get("present")
    if ascend_present:
        if "get_artifact_manifest" not in have:
            out.insert(0, {"tool": "get_artifact_manifest"})
            have.add("get_artifact_manifest")
        if "search_artifacts" not in have:
            out.insert(
                0,
                {
                    "tool": "search_artifacts",
                    "pattern": (
                        r"Traceback|Exception|Error|RuntimeError|ValueError|"
                        r"AssertionError|fatal error|must be|"
                        r"FAILED|ACL|CANN|HCCL|OOM|"
                        r"CrashLoopBackOff|ImagePullBackOff|FailedMount|"
                        r"Unschedulable"
                    ),
                    "max_matches": 30,
                },
            )
            have.add("search_artifacts")

    return out


def run_diagnosis(
    cfg: AgentConfig,
    log_file: Path,
    step_name: str,
    user_hint: str | None = None,
    artifact_dir: Path | None = None,
    git_context: dict[str, Any] | None = None,
    k8s_dir: Path | None = None,
    benchmark_dir: Path | None = None,
    *,
    index: dict[str, Any] | None = None,
    collector: EvidenceCollector | None = None,
) -> dict[str, Any]:
    """Run the bounded LLM diagnosis loop. Caller must verify ``cfg.llm_ready()``.

    ``index`` and ``collector`` are optional; when provided from ``main()`` the
    caller already built them (plus baseline evidence), so we skip the duplicate
    pass.
    """
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

    _log("init", f"log={log_file} size={log_file.stat().st_size} model={cfg.model} max_rounds={cfg.max_rounds}")

    if index is None:
        _log("index", "building deterministic log index")
        index = build_index(
            log_file,
            artifact_dir=artifact_dir,
            benchmark_dir=benchmark_dir,
            k8s_dir=k8s_dir,
            git_context=git_context,
        )
        _log(
            "index",
            f"total_lines={index.get('total_lines')} failed_tests={len(index.get('failed_tests') or [])} "
            f"wrappers={len(index.get('wrapper_hits') or [])} first_tb={index.get('first_traceback')}",
        )
    if collector is None:
        collector = EvidenceCollector(
            log_file=log_file,
            artifact_dir=artifact_dir,
            k8s_dir=k8s_dir,
            benchmark_dir=benchmark_dir,
        )
    else:
        _log("evidence", f"reusing collector with {len(collector.collected)} pre-existing payload(s)")

    messages: list[dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_round1_user(index, step_name, user_hint)},
    ]

    # --- round 1: routing ---
    _log("round1", f"sending routing request (prompt_chars~={len(messages[-1]['content'])})")
    try:
        reply = _chat_one(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            messages=messages,
            timeout_s=cfg.timeout_s,
            max_input_chars=cfg.max_input_chars,
        )
    except RuntimeError as exc:
        _log("round1", f"failed: {type(exc).__name__}: {exc}")
        return _skip_diagnosis(f"LLM request failed in round 1: {type(exc).__name__}: {exc}", step_name)
    _log("round1", f"received routing reply chars={len(reply)}")

    routing_raw = _parse_json_block(reply) or {}
    routing = _sanitize_routing(routing_raw)
    # Force minimum evidence requests so the agent cannot skip them.
    routing["evidence_requests"] = _enforce_min_evidence_requests(
        index, routing["evidence_requests"]
    )[:MAX_EVIDENCE_REQUESTS]
    _log(
        "round1",
        f"routing stage={routing['failure_stage']} layer={routing['failure_layer']} "
        f"playbooks={routing['matched_playbooks']} evidence_requests={len(routing['evidence_requests'])}",
    )

    # --- evidence retrieval (always before hypothesis) ---
    for req in routing["evidence_requests"]:
        tool = req.get("tool", "?")
        _log("evidence", f"fetching {tool} { {k: v for k, v in req.items() if k != 'tool'} }")
        collector.fetch(req)
    _log("evidence", f"collected {len(collector.collected)} payload(s)")

    # --- round 2: hypothesis + final arbitration ---
    messages.append({"role": "assistant", "content": reply})
    messages.append({
        "role": "user",
        "content": _build_round2_user(index, routing, collector.collected),
    })
    _log("round2", f"sending hypothesis request (prompt_chars~={len(messages[-1]['content'])})")
    try:
        reply2 = _chat_one(
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            model=cfg.model,
            messages=messages,
            timeout_s=cfg.timeout_s,
            max_input_chars=cfg.max_input_chars,
        )
    except RuntimeError as exc:
        _log("round2", f"failed: {type(exc).__name__}: {exc}")
        return _skip_diagnosis(f"LLM request failed in round 2: {type(exc).__name__}: {exc}", step_name)
    _log("round2", f"received hypothesis reply chars={len(reply2)}")

    # Parse the FINAL block (HYPOTHESES + FINAL pair, we take FINAL when present).
    # Use bracket counting to correctly handle nested JSON objects/arrays.
    final_raw: dict[str, Any] | None = None
    for candidate in _extract_json_blocks(reply2):
        try:
            obj = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "root_cause" in obj:
            final_raw = obj
            break
    if final_raw is None:
        final_raw = _parse_json_block(reply2) or {}

    diagnosis = _sanitize_diagnosis(
        final_raw,
        routing,
        failed_tests=index.get("failed_tests") or [],
    )
    diagnosis.update({
        "schema_version": SCHEMA_VERSION,
        "step_name": step_name,
        "log_file": str(log_file),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
    })
    return diagnosis


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_markdown(diag: dict[str, Any]) -> str:
    routing = diag.get("routing") or {}
    lines: list[str] = []
    lines.append(f"## CI AI 失败定位")
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

    lines.append(f"### Root cause")
    lines.append(diag.get("root_cause") or "_(no claim)_")
    lines.append("")

    if diag.get("evidence"):
        lines.append("### Evidence")
        lines.append("| file | line | snippet | interpretation |")
        lines.append("|---|---|---|---|")
        for e in diag["evidence"]:
            lines.append(
                f"| {e.get('file','')} | {e.get('line','')} | `{e.get('snippet','')}` | {e.get('interpretation','')} |"
            )
        lines.append("")

    if diag.get("counter_evidence"):
        lines.append("### Counter-evidence")
        lines.append("| file | line | snippet | interpretation |")
        lines.append("|---|---|---|---|")
        for e in diag["counter_evidence"]:
            lines.append(
                f"| {e.get('file','')} | {e.get('line','')} | `{e.get('snippet','')}` | {e.get('interpretation','')} |"
            )
        lines.append("")

    if diag.get("next_actions"):
        lines.append("### Next actions")
        lines.append("| priority | action | command |")
        lines.append("|---|---|---|")
        for a in diag["next_actions"]:
            lines.append(f"| {a.get('priority','')} | {a.get('action','')} | `{a.get('command','')}` |")
        lines.append("")

    if routing.get("wrapper_errors"):
        lines.append("### Wrapper errors (symptoms, not root cause)")
        for w in routing["wrapper_errors"]:
            lines.append(f"- L{w.get('line','')} {w.get('type','')}: `{w.get('snippet','')}`")
        lines.append("")

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
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
                        "collected_evidence": [],
                        "missing_llm_config": ["log file missing or empty"],
                    }
                )
            )
        return 0

    _log("init", f"log={args.log_file} size={args.log_file.stat().st_size}")

    _log("index", "building deterministic log index")
    index = build_index(
        args.log_file,
        artifact_dir=args.artifact_dir,
        benchmark_dir=args.benchmark_dir,
        k8s_dir=args.k8s_dir,
        git_context=git_context if any(git_context.values()) else None,
    )
    collector = EvidenceCollector(
        log_file=args.log_file,
        artifact_dir=args.artifact_dir,
        k8s_dir=args.k8s_dir,
        benchmark_dir=args.benchmark_dir,
    )
    _collect_baseline_evidence(index, collector)
    _log("evidence", f"collected {len(collector.collected)} baseline evidence item(s)")

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
            index=index,
            collector=collector,
        )
        summary_md = render_markdown(diag)
    else:
        missing = cfg.missing_llm_config()
        _log("llm", f"skipped: {', '.join(missing)}")
        diag = _make_evidence_only_diagnosis(
            step_name=args.step_name,
            log_file=args.log_file,
            index=index,
            collector=collector,
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


if __name__ == "__main__":
    raise SystemExit(main())
