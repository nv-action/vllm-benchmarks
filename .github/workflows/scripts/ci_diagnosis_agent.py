"""
CI AI diagnosis: evidence bundle builder.

This is the CLI entry point that builds a deterministic evidence bundle from
a failed CI job's log file. It collects:

1. A structured log index (via ``ci_diagnosis_index.build_index``).
2. Default evidence: first exception context, failure blocks, artifact
   manifest, benchmark summary, K8s summary (when directories are provided).
3. Git context (branch, commit, changed files) when a repo directory is
   available.

The output is a JSON bundle consumed by the LLM-based diagnosis agent
(loaded by the ``ci-ai-diagnosis-agent`` skill). This module does NOT make
LLM calls. It is a purely deterministic evidence pipeline.

Configuration (env vars, prefixed by VLLM_ASCEND_CI_AI_DIAGNOSIS_*):

    VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED       default 0

Modes:
- Enabled (ENABLED=1): build index + collect evidence + output JSON.
- Disabled: write a simple skip JSON with ``diagnosis_skipped=true``.
- The tool NEVER breaks CI: every path exits 0.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Ensure sibling scripts are importable when run as a CLI from the
# workflow scripts directory.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from ci_diagnosis_evidence import (  # noqa: E402
    get_artifact_manifest,
    get_benchmark_summary,
    get_failure_block,
    get_first_exception_context,
    get_k8s_summary,
    get_wrapper_upstream_context,
    search,
    search_artifacts,
)
from ci_diagnosis_index import build_index  # noqa: E402

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


@dataclass
class AgentConfig:
    enabled: bool

    @classmethod
    def from_env(cls) -> AgentConfig:
        try:
            import vllm_ascend.envs as envs

            return cls(
                enabled=bool(int(str(envs.VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED))),
            )
        except (ImportError, AttributeError):
            return cls(
                enabled=bool(int(os.getenv("VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED", "0"))),
            )


# ---------------------------------------------------------------------------
# Evidence collector
# ---------------------------------------------------------------------------


@dataclass
class EvidenceCollector:
    log_file: Path
    artifact_dir: Path | None = None
    k8s_dir: Path | None = None
    benchmark_dir: Path | None = None
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
                from ci_diagnosis_evidence import get_window

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
                from ci_diagnosis_evidence import get_last_exception_context

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
                from ci_diagnosis_evidence import list_artifacts

                if self.artifact_dir is None:
                    payload = {"artifact_dir": "", "files": []}
                else:
                    payload = list_artifacts(self.artifact_dir)
            elif tool == "get_artifact_window":
                from ci_diagnosis_evidence import get_artifact_window

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
        except Exception as exc:  # noqa: BLE001 - evidence is best-effort
            payload = {"error": f"{type(exc).__name__}: {exc}"}

        entry = {"request": request, "payload": payload}
        self.collected.append(entry)
        return entry

    def collect_default(self) -> None:
        """Auto-collect default evidence from the log and artifact dirs.

        This provides a baseline evidence set for the LLM agent without
        requiring a routing round first.
        """
        # 1. First exception context (always useful).
        self.fetch({"tool": "get_first_exception_context", "before": 40, "after": 120})

        # 2. Wrapper upstream context (if wrapper errors exist).
        self.fetch({"tool": "get_wrapper_upstream_context", "before": 200, "after": 40})

        # 3. Artifact manifest (if artifact_dir is set).
        if self.artifact_dir is not None:
            self.fetch({"tool": "get_artifact_manifest"})

        # 4. Benchmark summary (if benchmark_dir is set).
        if self.benchmark_dir is not None:
            self.fetch({"tool": "get_benchmark_summary"})

        # 5. K8s summary (if k8s_dir is set).
        if self.k8s_dir is not None:
            self.fetch({"tool": "get_k8s_summary"})


# ---------------------------------------------------------------------------
# Git context
# ---------------------------------------------------------------------------


def _build_git_context(
    repo_dir: Path | None,
    ref: str | None,
    sha: str | None,
) -> dict[str, Any]:
    """Extract git context for code-aware diagnosis.

    Returns ``ref``, ``sha``, ``commit`` (author/date/subject), and
    ``changed_files``. All git calls are best-effort and time-bounded.
    """
    ctx: dict[str, Any] = {"ref": ref or "", "sha": sha or ""}
    if repo_dir is None or not repo_dir.is_dir():
        return ctx
    try:
        log = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                "-1",
                "--format=%H%n%an%n%ad%n%s",
                sha or "HEAD",
            ],
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
    except Exception:
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
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def _log(stage: str, msg: str) -> None:
    sys.stderr.write(f"[ci-ai-diagnosis] {stage}: {msg}\n")
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _build_default_evidence(index: dict[str, Any], collector: EvidenceCollector) -> None:
    """Collect default evidence based on index content and available dirs."""
    failed_tests = index.get("failed_tests") or []
    if failed_tests:
        collector.fetch(
            {
                "tool": "get_failure_block",
                "test": failed_tests[0],
                "before": 5,
                "after": 200,
            }
        )

    wrapper_hits = index.get("wrapper_hits") or []
    if wrapper_hits:
        collector.fetch({"tool": "get_wrapper_upstream_context", "before": 200, "after": 40})

    if index.get("first_traceback"):
        collector.fetch({"tool": "get_first_exception_context", "before": 40, "after": 120})

    if collector.artifact_dir is not None:
        collector.fetch({"tool": "get_artifact_manifest"})

    if collector.benchmark_dir is not None:
        collector.fetch({"tool": "get_benchmark_summary"})

    if collector.k8s_dir is not None:
        collector.fetch({"tool": "get_k8s_summary"})


def _make_skip_bundle(step_name: str, reason: str) -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "type": "evidence_bundle",
        "step_name": step_name,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "diagnosis_skipped": True,
        "skip_reason": reason,
    }


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Build CI diagnosis evidence bundle (vllm-ascend).")
    p.add_argument("--log-file", type=Path, required=True)
    p.add_argument("--step-name", default="tests")
    p.add_argument("--artifact-dir", type=Path, default=None, help="General artifact directory (ascend logs, misc).")
    p.add_argument("--k8s-dir", type=Path, default=None, help="K8s diagnostics directory (pods.json, events).")
    p.add_argument("--benchmark-dir", type=Path, default=None, help="Benchmark results JSON directory.")
    p.add_argument("--ref", default=None, help="Git ref (branch/tag) being tested.")
    p.add_argument("--sha", default=None, help="Git commit SHA being tested.")
    p.add_argument("--repo-dir", type=Path, default=None, help="Path to checked-out git repo for context extraction.")
    p.add_argument("--output-json", type=Path, default=None)
    p.add_argument("--write-summary", action="store_true", help="Write evidence bundle summary to GITHUB_STEP_SUMMARY.")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    cfg = AgentConfig.from_env()

    if not cfg.enabled:
        _log("skip", "diagnosis disabled (VLLM_ASCEND_CI_AI_DIAGNOSIS_ENABLED != 1)")
        bundle = _make_skip_bundle(args.step_name, "diagnosis disabled")
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.write_summary:
            path = os.environ.get("GITHUB_STEP_SUMMARY")
            if path:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("### CI AI Diagnosis: skipped (disabled)\n\n")
            else:
                sys.stdout.write(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")
        return 0

    if not args.log_file.exists() or args.log_file.stat().st_size == 0:
        _log("skip", f"log file missing or empty: {args.log_file}")
        bundle = _make_skip_bundle(args.step_name, "log file missing or empty")
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        if args.write_summary:
            path = os.environ.get("GITHUB_STEP_SUMMARY")
            if path:
                with open(path, "a", encoding="utf-8") as fh:
                    fh.write("### CI AI Diagnosis: skipped (no log file)\n\n")
        return 0

    _log("init", f"log={args.log_file} size={args.log_file.stat().st_size}")

    # 1. Build git context.
    git_context = _build_git_context(
        repo_dir=args.repo_dir,
        ref=args.ref,
        sha=args.sha,
    )
    if git_context.get("commit", {}).get("subject"):
        _log("git", f"commit: {git_context['commit']['subject'][:80]}")
    changed = git_context.get("changed_files") or []
    if changed:
        _log("git", f"changed_files={len(changed)} e.g. {changed[0]}")

    # 2. Build log index.
    _log("index", "building log index")
    try:
        index = build_index(
            args.log_file,
            artifact_dir=args.artifact_dir,
            git_context=git_context if any(git_context.values()) else None,
        )
    except Exception as exc:
        _log("index", f"build_index failed: {type(exc).__name__}: {exc}")
        bundle = _make_skip_bundle(args.step_name, f"index building failed: {exc}")
        if args.output_json is not None:
            args.output_json.parent.mkdir(parents=True, exist_ok=True)
            args.output_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return 0

    _log(
        "index",
        f"total_lines={index.get('total_lines')} failed_tests={len(index.get('failed_tests') or [])} "
        f"wrappers={len(index.get('wrapper_hits') or [])} first_tb={index.get('first_traceback')}",
    )

    # 3. Collect default evidence.
    collector = EvidenceCollector(
        log_file=args.log_file,
        artifact_dir=args.artifact_dir,
        k8s_dir=args.k8s_dir,
        benchmark_dir=args.benchmark_dir,
    )
    _build_default_evidence(index, collector)
    _log("evidence", f"collected {len(collector.collected)} evidence item(s)")

    # 4. Build evidence bundle.
    bundle: dict[str, Any] = {
        "schema_version": "1.0",
        "type": "evidence_bundle",
        "step_name": args.step_name,
        "log_file": str(args.log_file),
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "git_context": git_context if any(git_context.values()) else None,
        "index": index,
        "evidence": collector.collected,
        "summary": {
            "total_evidence": len(collector.collected),
            "has_traceback": index.get("first_traceback") is not None,
            "has_failed_tests": len(index.get("failed_tests") or []) > 0,
            "has_wrapper_hits": len(index.get("wrapper_hits") or []) > 0,
            "has_artifacts": index.get("artifacts") is not None and len(index.get("artifacts") or []) > 0,
            "diagnosis_dispatched": False,
        },
    }

    # 5. Write output.
    if args.output_json is not None:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        _log("output", f"wrote evidence bundle to {args.output_json}")

    if args.write_summary:
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            summary_md = (
                "## CI AI Diagnosis Evidence Bundle\n\n"
                f"- **Step**: {args.step_name}\n"
                f"- **Log**: `{args.log_file}`\n"
                f"- **Total lines**: {index.get('total_lines')}\n"
                f"- **Failed tests**: {len(index.get('failed_tests') or [])}\n"
                f"- **Wrapper hits**: {len(index.get('wrapper_hits') or [])}\n"
                f"- **Evidence items**: {len(collector.collected)}\n"
                f"- **Diagnosis dispatched**: false\n"
            )
            with open(summary_path, "a", encoding="utf-8") as fh:
                fh.write(summary_md)
        else:
            # Fallback: print JSON to stdout.
            sys.stdout.write(json.dumps(bundle, ensure_ascii=False, indent=2) + "\n")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
