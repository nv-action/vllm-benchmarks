"""
CI AI diagnosis: deterministic log indexer (see
``.agents/skills/ci-ai-diagnosis-agent/SKILL.md``).

This script does NOT judge what is the root cause and does NOT pre-pick
"the most important lines". It only produces a structured *map* of the
log so the diagnosis agent (an LLM with evidence API access) can decide
what to look at next.

Output is a single JSON document. The schema is documented in the
``INDEX_SCHEMA`` constant below. The script is import-safe: it can be
used as a Python module by the agent and as a CLI by CI workflows.

The agent's contract:

    index = build_index(Path("ci.log"))
    # index["first_exception"]["line"] is just a number, not a verdict.
    # The agent reads index + uses ci_diagnosis_evidence to fetch windows.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import regex as re

# ---------------------------------------------------------------------------
# Patterns
# ---------------------------------------------------------------------------

# Keep this list small and explicit. The indexer MUST stay deterministic
# and reviewable; the agent does interpretation.
# All patterns are unanchored so they survive GitHub Actions' `2026-...Z `
# timestamp prefix in the captured job log.
_PYTEST_SUMMARY_BANNER = re.compile(r"=+\s+short test summary info\s+=+\s*$", re.IGNORECASE)
_PYTEST_FAILURES_BANNER = re.compile(r"=+\s+FAILURES\s+=+\s*$", re.IGNORECASE)
_PYTEST_FAILED_LINE = re.compile(r"\bFAILED\s+(tests/\S+::\S+)")
_PYTEST_COLLECTING = re.compile(r"\b(?:ERROR collecting|ImportError while loading conftest)\b")
_TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):")

# Wrapper / symptom errors. Indexer marks their positions; the agent
# decides whether they are root cause (almost always: no).
_WRAPPER_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("EngineDeadError", re.compile(r"\bEngineDeadError\b")),
    ("EngineCoreFatal", re.compile(r"EngineCore encountered a(?: fatal)? error", re.IGNORECASE)),
    ("CalledProcessError", re.compile(r"subprocess\.CalledProcessError")),
    ("TimeoutError", re.compile(r"\bTimeoutError\b|\btimed out\b", re.IGNORECASE)),
    ("InternalServerError500", re.compile(r"\b500\s+Internal Server Error\b")),
    ("CrashedWorker", re.compile(r"\bsegfault\b|core dumped|\bSIGSEGV\b", re.IGNORECASE)),
    ("K8sCrashLoopBackOff", re.compile(r"\bCrashLoopBackOff\b")),
    ("K8sImagePullBackOff", re.compile(r"\bImagePullBackOff\b|\bErrImagePull\b")),
    ("K8sOOMKilled", re.compile(r"\bOOMKilled\b")),
    ("ExitNonZero", re.compile(r"exited with (?:non-zero|code [^0])")),
    # Domain-specific cascade wrappers observed in vllm-ascend nightly logs.
    # These are always symptoms; the agent must trace them upstream.
    ("AISBenchRuntimeError", re.compile(r"\bAISBenchRuntimeError\b")),
    ("FileMatchError", re.compile(r"\bFileMatchError\b")),
    ("TinferRuntimeError", re.compile(r"\bTINFER-RUNTIME-\d+\b")),
    ("SummFileError", re.compile(r"\bSUMM-FILE-\d+\b")),
    ("AisbenchAssertion", re.compile(r"some aisbench cases failed")),
    ("PytestAssertionError", re.compile(r"\bAssertionError\b(?!\s*$)")),
)

# Domain pattern hits. Listed by layer to help routing (see the skill's
# failure_layer dimension).
_DOMAIN_PATTERNS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("acl", "npu_runtime", re.compile(r"\bACL\b|aclrt|\bascend\b", re.IGNORECASE)),
    ("cann", "npu_runtime", re.compile(r"\bCANN\b", re.IGNORECASE)),
    ("hccl", "npu_runtime", re.compile(r"\bHCCL\b|\bhccl\b")),
    ("oom", "npu_runtime", re.compile(r"out of memory|\bOOM\b", re.IGNORECASE)),
    ("timeout", "vllm_engine", re.compile(r"\btimeout\b|\btimed out\b", re.IGNORECASE)),
    ("vllm_engine", "vllm_engine", re.compile(r"EngineCore|sample_tokens|RPC call to", re.IGNORECASE)),
    ("vllm_ascend", "vllm_ascend", re.compile(r"vllm[-_]ascend|NPUModelRunner|Ascend[A-Z]\w*")),
    ("kubernetes", "kubernetes", re.compile(r"\bPod\b|\bkubectl\b|\bkubelet\b|\bnamespace\b")),
    ("pytest", "pytest", re.compile(r"=+\s+(?:FAILURES|short test summary info|ERRORS|PASSED)")),
    ("dependency", "dependency", re.compile(r"ImportError|ModuleNotFoundError|Requires-Python", re.IGNORECASE)),
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Span:
    """Inclusive line range in the log."""

    start: int  # 1-based
    end: int  # 1-based

    def as_dict(self) -> dict[str, int]:
        return {"start": self.start, "end": self.end}


@dataclass
class Index:
    """In-memory structured log map. Serialised to JSON by `to_dict`."""

    source_path: str
    total_lines: int = 0
    file_size: int = 0
    truncated: bool = False
    failed_tests: list[str] = field(default_factory=list)
    pytest_summary_span: Span | None = None
    pytest_failure_spans: list[Span] = field(default_factory=list)
    first_traceback: Span | None = None
    last_traceback: Span | None = None
    wrapper_hits: list[dict[str, Any]] = field(default_factory=list)
    pattern_hits: dict[str, list[int]] = field(default_factory=dict)
    pattern_hit_count: dict[str, int] = field(default_factory=dict)
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "1.0",
            "source_path": self.source_path,
            "total_lines": self.total_lines,
            "file_size": self.file_size,
            "truncated": self.truncated,
            "failed_tests": sorted(set(self.failed_tests)),
            "pytest_summary_span": self.pytest_summary_span.as_dict() if self.pytest_summary_span else None,
            "pytest_failure_spans": [s.as_dict() for s in self.pytest_failure_spans],
            "first_traceback": self.first_traceback.as_dict() if self.first_traceback else None,
            "last_traceback": self.last_traceback.as_dict() if self.last_traceback else None,
            "wrapper_hits": self.wrapper_hits,
            "pattern_hits": self.pattern_hits,
            "pattern_hit_count": dict(self.pattern_hit_count),
            "artifacts": self.artifacts,
        }


# ---------------------------------------------------------------------------
# Indexer
# ---------------------------------------------------------------------------


class LogIndexer:
    """Walk a log once and produce a deterministic Index.

    The indexer must not perform classification beyond what a regex
    naturally implies. "Failed test name" is a regex match; "this is the
    root cause" is a verdict, and the indexer never emits verdicts.
    """

    def __init__(self, *, max_pattern_hits_per_kind: int = 50) -> None:
        self._max_pattern_hits = max_pattern_hits_per_kind
        self._counts: Counter[str] = Counter()
        self._first_tb: Span | None = None
        self._last_tb: Span | None = None
        self._tb_open: int | None = None
        self._pytest_summary_open: int | None = None
        self._pytest_failure_open: int | None = None

    def build(self, log_path: Path, *, artifact_dir: Path | None = None) -> Index:
        index = Index(
            source_path=str(log_path),
            file_size=log_path.stat().st_size if log_path.exists() else 0,
        )
        if not log_path.exists():
            return index

        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        index.total_lines = len(lines)

        for i, raw in enumerate(lines, start=1):
            line = raw.rstrip("\n")
            self._scan_pytest(i, line, index)
            self._scan_tracebacks(i, line)
            self._scan_wrappers(i, line, index)
            self._scan_patterns(i, line, index)

        self._finalize(index)

        if artifact_dir is not None and artifact_dir.is_dir():
            index.artifacts = self._scan_artifacts(artifact_dir)

        return index

    # --- sub-scanners ----------------------------------------------------

    def _scan_pytest(self, line_no: int, line: str, index: Index) -> None:
        m = _PYTEST_FAILED_LINE.search(line)
        if m:
            index.failed_tests.append(m.group(1))

        if _PYTEST_SUMMARY_BANNER.search(line):
            self._pytest_summary_open = line_no

        if _PYTEST_FAILURES_BANNER.search(line):
            self._pytest_failure_open = line_no
        elif self._pytest_failure_open is not None and line.startswith("=") and self._pytest_failure_open != line_no:
            index.pytest_failure_spans.append(Span(start=self._pytest_failure_open, end=line_no - 1))
            self._pytest_failure_open = None

        if _PYTEST_COLLECTING.search(line):
            index.pattern_hits.setdefault("pytest_collection_error", []).append(line_no)
            self._counts["pytest_collection_error"] += 1

    def _scan_tracebacks(self, line_no: int, line: str) -> None:
        if _TRACEBACK_START.search(line):
            if self._tb_open is None:
                self._first_tb = Span(start=line_no, end=line_no)
            self._tb_open = line_no
            return
        if self._tb_open is not None and line.startswith(" ") is False and line.strip():
            # End of an indented traceback block.
            self._last_tb = Span(start=self._tb_open, end=line_no - 1)
            self._tb_open = None

    def _scan_wrappers(self, line_no: int, line: str, index: Index) -> None:
        for kind, pat in _WRAPPER_PATTERNS:
            if pat.search(line):
                snippet = line.strip()
                if len(snippet) > 240:
                    snippet = snippet[:237] + "..."
                index.wrapper_hits.append({"type": kind, "line": line_no, "snippet": snippet})
                self._counts[f"wrapper:{kind}"] += 1
                break  # one wrapper type per line is enough

    def _scan_patterns(self, line_no: int, line: str, index: Index) -> None:
        for name, _layer, pat in _DOMAIN_PATTERNS:
            if pat.search(line):
                hits = index.pattern_hits.setdefault(name, [])
                if len(hits) < self._max_pattern_hits:
                    hits.append(line_no)
                self._counts[name] += 1

    def _finalize(self, index: Index) -> None:
        index.first_traceback = self._first_tb
        index.last_traceback = self._last_tb
        index.pattern_hit_count = dict(self._counts)

        if self._pytest_summary_open is not None:
            index.pytest_summary_span = Span(start=self._pytest_summary_open, end=index.total_lines)

        if self._pytest_failure_open is not None:
            # banner that never closed before EOF
            index.pytest_failure_spans.append(Span(start=self._pytest_failure_open, end=index.total_lines))

        if self._tb_open is not None:
            self._last_tb = Span(start=self._tb_open, end=index.total_lines)
            index.last_traceback = self._last_tb

    @staticmethod
    def _scan_artifacts(root: Path) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for p in sorted(root.rglob("*")):
            if p.is_file():
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                rel = p.relative_to(root)
                out.append({"path": str(rel), "size": size})
        return out


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

INDEX_SCHEMA = "1.0"


def build_index(
    log_path: Path,
    *,
    artifact_dir: Path | None = None,
    git_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build and return the index as a JSON-serialisable dict.

    When ``artifact_dir`` is provided, the indexer delegates the typed
    artifact parsing to ``ci_diagnosis_artifacts`` and attaches a
    deterministic manifest + summary alongside the existing
    ``artifacts: [{path,size}]`` file list. Missing artifact sources
    appear in the summary with ``present=False`` so the agent can
    distinguish "no benchmark JSON" from "benchmark passed".

    When ``git_context`` is provided (a dict with keys like ``ref``,
    ``sha``, ``commit``, ``changed_files``), it is attached to the index
    so the agent can correlate failures with code changes.
    """
    index = LogIndexer().build(log_path, artifact_dir=artifact_dir).to_dict()
    if artifact_dir is not None:
        # Local import keeps the indexer importable in environments
        # where the artifact parser is not on the path.
        from ci_diagnosis_artifacts import (  # type: ignore
            build_manifest,
            summarize_benchmark_results,
            summarize_k8s_state,
        )

        index["artifact_manifest"] = build_manifest(Path(artifact_dir))
        index["artifact_summary"] = {
            "benchmark": summarize_benchmark_results(Path(artifact_dir)),
            "k8s": summarize_k8s_state(Path(artifact_dir)),
        }
    if git_context:
        index["git_context"] = git_context
    return index


def iter_lines(log_path: Path) -> Iterable[tuple[int, str]]:
    """Yield (line_no, line) pairs for evidence API callers."""
    if not log_path.exists():
        return
    with log_path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, start=1):
            yield i, line.rstrip("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build a deterministic log index for CI AI diagnosis.")
    p.add_argument("--log-file", type=Path, required=True, help="Path to the CI log file.")
    p.add_argument("--artifact-dir", type=Path, default=None, help="Optional directory of related artifacts.")
    p.add_argument("--output", type=Path, default=None, help="Where to write the JSON. Defaults to stdout.")
    p.add_argument("--pretty", action="store_true", help="Pretty-print the JSON output.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    idx = build_index(args.log_file, artifact_dir=args.artifact_dir)
    payload = json.dumps(idx, ensure_ascii=False, indent=2 if args.pretty else None, sort_keys=True)
    if args.output is None:
        sys.stdout.write(payload + "\n")
    else:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
