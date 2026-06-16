"""
CI AI diagnosis: evidence API (see
``.agents/skills/ci-ai-diagnosis-agent/SKILL.md``).

The diagnosis agent is the *brain*; this module is its *hands*. It
provides deterministic, line-accurate evidence extraction. It does NOT
judge what is the root cause.

Tools (Python API and CLI):

    get_window(line, before, after)
    search(pattern, max_matches)
    get_failure_block(test_name)
    get_first_exception_context(before, after)
    get_last_exception_context(before, after)
    get_wrapper_upstream_context(before, after)
    list_artifacts(artifact_dir)
    get_artifact_window(artifact, line, before, after)

All tools return either a dict or a list. None of them fabricate content
that is not in the file. The agent is responsible for *interpretation*.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import regex as re

# Make the sibling indexer importable when this file is run as a CLI
# from the workflow scripts directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

# Importing the indexer is safe and side-effect free.
from ci_diagnosis_index import build_index  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DEFAULT_BEFORE = 40
DEFAULT_AFTER = 80
MAX_OUTPUT_CHARS = 200_000  # hard ceiling per call to keep agent loop bounded


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class _LogReader:
    path: Path
    lines: list[str] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.path.exists():
            self.lines = []
            return
        self.lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()

    @property
    def total(self) -> int:
        return 0 if self.lines is None else len(self.lines)

    def window(self, line: int, before: int, after: int) -> dict[str, Any]:
        if self.total == 0:
            return {"file": str(self.path), "start": 0, "end": 0, "text": ""}
        line = max(1, min(line, self.total))
        start = max(1, line - before)
        end = min(self.total, line + after)
        text = "\n".join(f"L{i}:{self.lines[i - 1]}" for i in range(start, end + 1))
        if len(text) > MAX_OUTPUT_CHARS:
            text = text[: MAX_OUTPUT_CHARS - 80] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars] ..."
        return {"file": str(self.path), "start": start, "end": end, "text": text}

    def search(self, pattern: str, max_matches: int = 50) -> dict[str, Any]:
        if self.total == 0:
            return {"file": str(self.path), "pattern": pattern, "matches": []}
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return {"file": str(self.path), "pattern": pattern, "matches": [], "error": str(exc)}
        out: list[dict[str, Any]] = []
        for i, raw in enumerate(self.lines, start=1):
            m = rx.search(raw)
            if m:
                out.append(
                    {
                        "line": i,
                        "match": m.group(0),
                        "snippet": raw.strip()[:240],
                    }
                )
                if len(out) >= max_matches:
                    break
        return {"file": str(self.path), "pattern": pattern, "matches": out}


def _open(path: Path) -> _LogReader:
    return _LogReader(path)


# ---------------------------------------------------------------------------
# Evidence tools
# ---------------------------------------------------------------------------


def get_window(log_file: Path, line: int, before: int = DEFAULT_BEFORE, after: int = DEFAULT_AFTER) -> dict[str, Any]:
    """Return a context window of `before+after+1` lines around `line`."""
    return _open(log_file).window(line, before, after)


def search(log_file: Path, pattern: str, max_matches: int = 50) -> dict[str, Any]:
    """Return regex matches with line numbers."""
    return _open(log_file).search(pattern, max_matches=max_matches)


def get_failure_block(log_file: Path, test_name: str, *, before: int = 5, after: int = 200) -> dict[str, Any]:
    """Return the pytest FAILURES block for a specific test.

    "Block" = from the test's separator line (`___ test_xxx ___`) up to
    the next separator / blank section break. If no block is found, the
    function returns an empty range.
    """
    reader = _open(log_file)
    if reader.total == 0:
        return {"file": str(log_file), "test": test_name, "start": 0, "end": 0, "text": ""}

    # pytest writes `____ test_name ____` as a separator. Match the
    # *test*'s suffix to be tolerant of parametrize.
    suffix = test_name.split("::")[-1]
    rx = re.compile(rf"_+(\s+){re.escape(suffix)}\1_+", re.IGNORECASE)
    start: int | None = None
    for i, line in enumerate(reader.lines, start=1):
        if rx.search(line):
            start = i
            break
    if start is None:
        return {"file": str(log_file), "test": test_name, "start": 0, "end": 0, "text": ""}

    end = reader.total
    for j in range(start + 1, reader.total + 1):
        raw = reader.lines[j - 1]
        # Another separator (`____ test_xxx ____`) ends the block.
        if raw.startswith("_") and "____" in raw:
            end = j - 1
            break
        # A `===` banner — either pure `==...` or `=== ... ===` —
        # ends the block. This covers FAILURES, ERRORS, short summary
        # info, PASSED headers.
        if re.match(r"^=+\s*\S.*=+\s*$", raw) or (raw.startswith("===") and raw.rstrip("=").strip() == ""):
            end = j - 1
            break
    # Apply before/after padding so the agent sees setup and follow-up
    # context, not just the failure block itself.
    start = max(1, start - before)
    end = min(reader.total, end + after)
    text = "\n".join(f"L{i}:{reader.lines[i - 1]}" for i in range(start, end + 1))
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[: MAX_OUTPUT_CHARS - 80] + f"\n... [truncated at {MAX_OUTPUT_CHARS} chars] ..."
    return {
        "file": str(log_file),
        "test": test_name,
        "start": start,
        "end": end,
        "text": text,
    }


def get_first_exception_context(
    log_file: Path, *, before: int = DEFAULT_BEFORE, after: int = DEFAULT_AFTER
) -> dict[str, Any]:
    """Window around the first `Traceback (most recent call last):`."""
    idx = build_index(log_file)
    span = idx.get("first_traceback")
    if not span:
        return {"file": str(log_file), "start": 0, "end": 0, "text": "", "note": "no traceback found"}
    line = span["start"]
    return get_window(log_file, line, before=before, after=after) | {"traceback_line": line}


def get_last_exception_context(
    log_file: Path, *, before: int = DEFAULT_BEFORE, after: int = DEFAULT_AFTER
) -> dict[str, Any]:
    """Window around the last traceback block."""
    idx = build_index(log_file)
    span = idx.get("last_traceback")
    if not span:
        return {"file": str(log_file), "start": 0, "end": 0, "text": "", "note": "no traceback found"}
    return get_window(log_file, span["start"], before=before, after=after) | {"traceback_line": span["start"]}


def get_wrapper_upstream_context(log_file: Path, *, before: int = 200, after: int = 40) -> dict[str, Any]:
    """For each wrapper hit, return a window that contains the upstream cause.

    Heuristic: take `before` lines before the wrapper and `after` lines
    after. The agent must interpret; this tool only delivers the
    candidates. We surface the first non-wrapper error in the window when
    we can find one.
    """
    idx = build_index(log_file)
    wrappers = idx.get("wrapper_hits") or []
    reader = _open(log_file)
    if reader.total == 0 or not wrappers:
        return {"file": str(log_file), "candidates": []}

    out: list[dict[str, Any]] = []
    for w in wrappers[:20]:  # cap to keep output bounded
        win = reader.window(w["line"], before, after)
        upstream = _first_non_wrapper_line(reader.lines, w["line"], w["type"])
        out.append(
            {
                "wrapper": w,
                "window": win,
                "suspected_upstream": upstream,
            }
        )
    return {"file": str(log_file), "candidates": out}


def _first_non_wrapper_line(lines: list[str], wrapper_line: int, wrapper_type: str) -> dict[str, Any] | None:
    """Walk backwards from the wrapper to find the first line that
    looks like an upstream cause (Traceback, ERROR, FAILED, or a
    different error type)."""
    from ci_diagnosis_index import _TRACEBACK_START, _WRAPPER_PATTERNS  # type: ignore

    wrapper_rx = next((p for t, p in _WRAPPER_PATTERNS if t == wrapper_type), None)

    for i in range(wrapper_line - 1, max(0, wrapper_line - 400), -1):
        raw = lines[i]
        if _TRACEBACK_START.match(raw):
            return {"line": i + 1, "snippet": raw.strip()[:240], "kind": "traceback_start"}
        if re.search(r"\b(AssertionError|RuntimeError|ValueError|OSError|ImportError|KeyError|TypeError)\b", raw):
            return {"line": i + 1, "snippet": raw.strip()[:240], "kind": "named_exception"}
        if re.search(r"^(?:ERROR|CRITICAL)\s*[\]:]", raw):
            return {"line": i + 1, "snippet": raw.strip()[:240], "kind": "level_error"}
        if wrapper_rx is not None and wrapper_rx.search(raw):
            # A wrapper of the same type is not upstream.
            continue
    return None


def list_artifacts(artifact_dir: Path) -> dict[str, Any]:
    if not artifact_dir.exists() or not artifact_dir.is_dir():
        return {"artifact_dir": str(artifact_dir), "files": []}
    files: list[dict[str, Any]] = []
    for p in sorted(artifact_dir.rglob("*")):
        if p.is_file():
            try:
                size = p.stat().st_size
            except OSError:
                size = 0
            files.append({"path": str(p.relative_to(artifact_dir)), "size": size})
    return {"artifact_dir": str(artifact_dir), "files": files}


def get_artifact_manifest(
    artifact_dir: Path,
    *,
    k8s_dir: Path | None = None,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the typed manifest, optionally aggregating across
    independent K8s / benchmark / ascend-logs directories.

    When ``k8s_dir`` or ``benchmark_dir`` are provided, their manifests
    are built separately and merged under the ``k8s`` / ``benchmark``
    keys. ``artifact_dir`` remains the primary source for ascend-logs
    and other general artifacts.
    """
    try:
        from ci_diagnosis_artifacts import build_manifest  # type: ignore

        primary = build_manifest(artifact_dir)
        if k8s_dir is not None and k8s_dir.is_dir():
            primary["k8s"] = build_manifest(k8s_dir).get("k8s", primary.get("k8s") or {"present": False})
            if primary["k8s"].get("present") and "dir" in primary.get("k8s", {}):
                primary["k8s"]["_source"] = str(k8s_dir)
        if benchmark_dir is not None and benchmark_dir.is_dir():
            primary["benchmark_results"] = build_manifest(benchmark_dir).get(
                "benchmark_results", primary.get("benchmark_results") or {"present": False}
            )
            if primary["benchmark_results"].get("present"):
                primary["benchmark_results"]["_source"] = str(benchmark_dir)
        return primary
    except (ImportError, ModuleNotFoundError):
        primary = list_artifacts(artifact_dir) | {"manifest_unavailable": True}
        if k8s_dir is not None and k8s_dir.is_dir():
            primary["k8s_files"] = list_artifacts(k8s_dir).get("files", [])
        if benchmark_dir is not None and benchmark_dir.is_dir():
            primary["benchmark_files"] = list_artifacts(benchmark_dir).get("files", [])
        return primary


def get_benchmark_summary(
    artifact_dir: Path,
    *,
    benchmark_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the benchmark_results JSON summary.

    When ``benchmark_dir`` is provided, it takes precedence over
    ``artifact_dir`` for locating benchmark JSON files.
    """
    source = benchmark_dir if benchmark_dir is not None else artifact_dir
    try:
        from ci_diagnosis_artifacts import (  # type: ignore
            ensure_unpacked,
            summarize_benchmark_results,
        )

        # benchmark results don't need tar unpacking — ensure_unpacked is
        # a no-op when the directory has files.
        return summarize_benchmark_results(source)
    except (ImportError, ModuleNotFoundError) as exc:
        return {"error": f"benchmark summary unavailable: {exc}"}


def get_k8s_summary(
    artifact_dir: Path,
    *,
    k8s_dir: Path | None = None,
) -> dict[str, Any]:
    """Return the K8s state summary.

    When ``k8s_dir`` is provided, it takes precedence over
    ``artifact_dir`` for locating K8s JSON / events files.
    """
    source = k8s_dir if k8s_dir is not None else artifact_dir
    try:
        from ci_diagnosis_artifacts import summarize_k8s_state  # type: ignore

        return summarize_k8s_state(source)
    except (ImportError, ModuleNotFoundError) as exc:
        return {"error": f"k8s summary unavailable: {exc}"}


def search_artifacts(
    artifact_dir: Path,
    pattern: str,
    *,
    max_matches: int = 50,
    extra_dirs: list[Path] | None = None,
) -> dict[str, Any]:
    """Cross-artifact regex search across the main artifact dir and
    optional extra directories (K8s, benchmark, etc.)."""
    try:
        from ci_diagnosis_artifacts import (  # type: ignore
            ensure_unpacked,
        )
        from ci_diagnosis_artifacts import (
            search_artifacts as _search_artifacts,
        )

        ensure_unpacked(artifact_dir)
        combined = _search_artifacts(artifact_dir, pattern, max_matches=max_matches)
        if extra_dirs:
            remaining = max_matches - len(combined.get("matches") or [])
            for extra in extra_dirs:
                if remaining <= 0:
                    break
                if extra is None or not extra.is_dir():
                    continue
                extra_result = _search_artifacts(extra, pattern, max_matches=remaining)
                extra_matches = extra_result.get("matches") or []
                if extra_matches:
                    combined.setdefault("matches", []).extend(extra_matches)
                    combined["matches"] = combined["matches"][:max_matches]
                remaining = max_matches - len(combined.get("matches") or [])
            if "extra_dirs_searched" not in combined:
                combined["extra_dirs_searched"] = [str(d) for d in extra_dirs if d is not None and d.is_dir()]
        return combined
    except (ImportError, ModuleNotFoundError) as exc:
        return {"error": f"artifact search unavailable: {exc}"}


def get_artifact_window(
    artifact: Path, line: int, before: int = DEFAULT_BEFORE, after: int = DEFAULT_AFTER
) -> dict[str, Any]:
    """Same as `get_window` but on a non-main artifact file."""
    return get_window(artifact, line, before=before, after=after)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Evidence API for the CI AI diagnosis agent.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("window")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--line", type=int, required=True)
    s.add_argument("--before", type=int, default=DEFAULT_BEFORE)
    s.add_argument("--after", type=int, default=DEFAULT_AFTER)

    s = sub.add_parser("search")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--pattern", required=True)
    s.add_argument("--max-matches", type=int, default=50)

    s = sub.add_parser("failure-block")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--test", required=True)

    s = sub.add_parser("first-exception")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--before", type=int, default=DEFAULT_BEFORE)
    s.add_argument("--after", type=int, default=DEFAULT_AFTER)

    s = sub.add_parser("last-exception")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--before", type=int, default=DEFAULT_BEFORE)
    s.add_argument("--after", type=int, default=DEFAULT_AFTER)

    s = sub.add_parser("wrapper-upstream")
    s.add_argument("--log-file", type=Path, required=True)
    s.add_argument("--before", type=int, default=200)
    s.add_argument("--after", type=int, default=40)

    s = sub.add_parser("list-artifacts")
    s.add_argument("--artifact-dir", type=Path, required=True)

    s = sub.add_parser("artifact-window")
    s.add_argument("--artifact", type=Path, required=True)
    s.add_argument("--line", type=int, required=True)
    s.add_argument("--before", type=int, default=DEFAULT_BEFORE)
    s.add_argument("--after", type=int, default=DEFAULT_AFTER)

    s = sub.add_parser("artifact-manifest")
    s.add_argument("--artifact-dir", type=Path, required=True)
    s.add_argument("--k8s-dir", type=Path, default=None)
    s.add_argument("--benchmark-dir", type=Path, default=None)

    s = sub.add_parser("benchmark-summary")
    s.add_argument("--artifact-dir", type=Path, required=True)
    s.add_argument("--benchmark-dir", type=Path, default=None)

    s = sub.add_parser("k8s-summary")
    s.add_argument("--artifact-dir", type=Path, required=True)
    s.add_argument("--k8s-dir", type=Path, default=None)

    s = sub.add_parser("artifact-search")
    s.add_argument("--artifact-dir", type=Path, required=True)
    s.add_argument("--pattern", required=True)
    s.add_argument("--max-matches", type=int, default=50)
    s.add_argument("--k8s-dir", type=Path, default=None)
    s.add_argument("--benchmark-dir", type=Path, default=None)

    args = p.parse_args(argv)
    if args.cmd == "window":
        _print_json(get_window(args.log_file, args.line, args.before, args.after))
    elif args.cmd == "search":
        _print_json(search(args.log_file, args.pattern, args.max_matches))
    elif args.cmd == "failure-block":
        _print_json(get_failure_block(args.log_file, args.test))
    elif args.cmd == "first-exception":
        _print_json(get_first_exception_context(args.log_file, before=args.before, after=args.after))
    elif args.cmd == "last-exception":
        _print_json(get_last_exception_context(args.log_file, before=args.before, after=args.after))
    elif args.cmd == "wrapper-upstream":
        _print_json(get_wrapper_upstream_context(args.log_file, before=args.before, after=args.after))
    elif args.cmd == "list-artifacts":
        _print_json(list_artifacts(args.artifact_dir))
    elif args.cmd == "artifact-window":
        _print_json(get_artifact_window(args.artifact, args.line, args.before, args.after))
    elif args.cmd == "artifact-manifest":
        _print_json(
            get_artifact_manifest(
                args.artifact_dir,
                k8s_dir=getattr(args, "k8s_dir", None),
                benchmark_dir=getattr(args, "benchmark_dir", None),
            )
        )
    elif args.cmd == "benchmark-summary":
        _print_json(
            get_benchmark_summary(
                args.artifact_dir,
                benchmark_dir=getattr(args, "benchmark_dir", None),
            )
        )
    elif args.cmd == "k8s-summary":
        _print_json(
            get_k8s_summary(
                args.artifact_dir,
                k8s_dir=getattr(args, "k8s_dir", None),
            )
        )
    elif args.cmd == "artifact-search":
        extra_dirs = [
            d
            for d in (getattr(args, "k8s_dir", None), getattr(args, "benchmark_dir", None))
            if d is not None and d.is_dir()
        ] or None
        _print_json(
            search_artifacts(
                args.artifact_dir,
                args.pattern,
                max_matches=args.max_matches,
                extra_dirs=extra_dirs,
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
