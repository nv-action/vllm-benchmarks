"""
Deterministic parser for CI diagnosis artifacts.

This module complements the main job-log indexer in
``ci_diagnosis_index.py``. It walks a directory of CI artifacts produced
by the multi-node nightly workflow (see
``_e2e_nightly_multi_node.yaml``) and produces a structured view of:

  * benchmark_results JSON files (accuracy / performance pass/fail)
  * kubernetes state (pods.json, events, per-pod describe/logs)
  * ascend_logs (collected per-node plog + pod stdout trees, plus the
    tar archive that was uploaded to GitHub Actions)

It is strictly deterministic. It does not classify failures or pick root
causes. The agent decides; this module only delivers evidence.

Public API:

    build_manifest(artifact_dir) -> dict
    ensure_unpacked(artifact_dir) -> Path
    summarize_benchmark_results(artifact_dir, ...) -> dict
    summarize_k8s_state(artifact_dir) -> dict
    search_artifacts(artifact_dir, pattern, max_matches) -> dict
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import Any

import regex as re

# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------

# Paths relative to the artifact root. We anchor on the canonical names
# produced by ``_e2e_nightly_multi_node.yaml`` rather than re-deriving
# them, so renaming a workflow step requires updating this map too.
BENCHMARK_GLOB = ("benchmark_results", "**", "*.json")
ASCEND_COLLECTED_GLOB = ("ascend_logs", "collected-logs")
ASCEND_TAR = Path("ascend_logs") / "ascend-logs.tar.gz"
K8S_GLOB = ("k8s",)


def build_manifest(artifact_dir: Path) -> dict[str, Any]:
    """Walk ``artifact_dir`` and return a typed file manifest.

    The manifest is what the agent sees in round 1 alongside the main
    job-log index. Missing directories are reported as ``present=False``
    instead of raising, because a missing evidence source is itself a
    diagnostic signal (e.g. "no benchmark JSON, so the test never
    reached the result-serialization step").
    """
    root = Path(artifact_dir)
    manifest: dict[str, Any] = {
        "root": str(root),
        "exists": root.exists() and root.is_dir(),
        "benchmark_results": {"present": False, "files": []},
        "ascend_logs": {
            "present": False,
            "tar": None,
            "collected_logs_dir": None,
            "files": [],
        },
        "k8s": {"present": False, "files": []},
    }
    if not root.exists() or not root.is_dir():
        return manifest

    bench_dir = root / "benchmark_results"
    if bench_dir.is_dir():
        files = sorted(p for p in bench_dir.rglob("*.json") if p.is_file())
        manifest["benchmark_results"] = {
            "present": bool(files),
            "dir": str(bench_dir.relative_to(root)) if files else None,
            "files": [str(p.relative_to(root)) for p in files],
        }

    ascend_tar = root / ASCEND_TAR
    collected_dir = root / ASCEND_COLLECTED_GLOB[0] / ASCEND_COLLECTED_GLOB[1]
    ascend_files: list[str] = []
    if collected_dir.is_dir():
        ascend_files.extend(str(p.relative_to(root)) for p in sorted(collected_dir.rglob("*")) if p.is_file())
    manifest["ascend_logs"] = {
        "present": bool(ascend_files) or ascend_tar.is_file(),
        "tar": str(ASCEND_TAR) if ascend_tar.is_file() else None,
        "collected_logs_dir": (str(Path(*ASCEND_COLLECTED_GLOB)) if collected_dir.is_dir() else None),
        "files": ascend_files,
    }

    k8s_dir = root / "k8s"
    if k8s_dir.is_dir():
        files = sorted(str(p.relative_to(root)) for p in k8s_dir.rglob("*") if p.is_file())
        manifest["k8s"] = {
            "present": bool(files),
            "dir": str(k8s_dir.relative_to(root)) if files else None,
            "files": files,
        }

    return manifest


# ---------------------------------------------------------------------------
# Tar extraction
# ---------------------------------------------------------------------------


def ensure_unpacked(artifact_dir: Path) -> Path:
    """Make sure ascend_logs/collected-logs exists.

    If the workflow only uploaded ``ascend-logs.tar.gz`` (or if the
    collected-logs tree is empty), extract the tar into a sibling
    ``collected-logs.unpacked`` directory so the agent can still walk
    per-node files. Returns the path to the populated directory.
    """
    root = Path(artifact_dir)
    collected_dir = root / ASCEND_COLLECTED_GLOB[0] / ASCEND_COLLECTED_GLOB[1]
    if collected_dir.is_dir() and any(collected_dir.rglob("*")):
        return collected_dir

    tar_path = root / ASCEND_TAR
    if not tar_path.is_file():
        return collected_dir  # may not exist; caller will see present=False

    unpacked = root / "ascend_logs" / "collected-logs.unpacked"
    unpacked.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:gz") as tf:
            for member in tf.getmembers():
                # Path-traversal guard: only accept members that live
                # under the archive's own root and never escape via "..".
                name = member.name
                if not name or name.startswith("/") or ".." in Path(name).parts:
                    continue
                target = unpacked / name
                if not str(target.resolve()).startswith(str(unpacked.resolve())):
                    continue
                if member.isdir():
                    target.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    data = tf.extractfile(member)
                    if data is None:
                        continue
                    try:
                        target.write_bytes(data.read())
                    finally:
                        data.close()
    except (tarfile.TarError, OSError):
        # Leave whatever was extracted; the manifest will reflect reality.
        pass
    return unpacked


# ---------------------------------------------------------------------------
# Benchmark JSON
# ---------------------------------------------------------------------------


def summarize_benchmark_results(
    artifact_dir: Path,
    *,
    max_tasks: int = 200,
) -> dict[str, Any]:
    """Read benchmark_results JSON files and produce a pass/fail summary.

    The schema is defined in
    ``tests/e2e/nightly/multi_node/scripts/benchmark_results.py``:
    ``tasks[].{name,metrics,target,pass_fail}`` plus
    ``{model_name, hardware, dtype, feature, vllm_version,
    vllm_ascend_version, serve_cmd, environment}``.

    We only return a deterministic summary; the agent decides what
    ``pass_fail=fail`` means for routing.
    """
    root = Path(artifact_dir)
    summary: dict[str, Any] = {
        "present": False,
        "files": [],
        "models": [],
        "hardware": [],
        "feature": [],
        "tasks_total": 0,
        "tasks_failed": 0,
        "failed_tasks": [],
        "first_failed_metric": None,
    }
    bench_dir = root / "benchmark_results"
    if not bench_dir.is_dir():
        return summary

    files = sorted(p for p in bench_dir.rglob("*.json") if p.is_file())
    summary["files"] = [str(p.relative_to(root)) for p in files]
    if not files:
        return summary

    summary["present"] = True
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if not isinstance(data, dict):
            continue

        if data.get("model_name"):
            summary["models"].append(str(data["model_name"]))
        if data.get("hardware"):
            summary["hardware"].append(str(data["hardware"]))
        if data.get("feature"):
            feat = data["feature"]
            if isinstance(feat, list):
                summary["feature"].extend(str(f) for f in feat)
            else:
                summary["feature"].append(str(feat))

        tasks = data.get("tasks") or []
        if not isinstance(tasks, list):
            continue
        for task in tasks:
            if not isinstance(task, dict):
                continue
            summary["tasks_total"] += 1
            if task.get("pass_fail") != "fail":
                continue
            summary["tasks_failed"] += 1
            metrics = task.get("metrics") or {}
            first_metric = next(iter(metrics), None)
            entry = {
                "name": str(task.get("name") or ""),
                "target": task.get("target") or {},
                "metrics": metrics,
                "source_file": str(path.relative_to(root)),
            }
            summary["failed_tasks"].append(entry)
            if summary["first_failed_metric"] is None and first_metric:
                expected = (task.get("target") or {}).get("baseline")
                threshold = (task.get("target") or {}).get("threshold")
                summary["first_failed_metric"] = {
                    "task": entry["name"],
                    "metric": first_metric,
                    "value": metrics.get(first_metric),
                    "baseline": expected,
                    "threshold": threshold,
                }

    # Dedup scalar lists while preserving order.
    summary["models"] = list(dict.fromkeys(summary["models"]))
    summary["hardware"] = list(dict.fromkeys(summary["hardware"]))
    summary["feature"] = list(dict.fromkeys(summary["feature"]))

    if summary["tasks_failed"] > max_tasks:
        summary["failed_tasks"] = summary["failed_tasks"][:max_tasks]
        summary["failed_tasks_truncated"] = True

    return summary


# ---------------------------------------------------------------------------
# Kubernetes state
# ---------------------------------------------------------------------------

_K8S_REASON_RX = re.compile(
    r"\b("
    r"CrashLoopBackOff|ImagePullBackOff|ErrImagePull|OOMKilled|"
    r"FailedScheduling|Unschedulable|FailedMount|FailedCreatePodSandbox|"
    r"BackOffPullingImage|Pending|Error|Evicted"
    r")\b"
)


def summarize_k8s_state(artifact_dir: Path) -> dict[str, Any]:
    """Summarize K8s state captured by ``Collect K8s diagnostics``.

    Pulls structured data from ``k8s/pods.json`` when available and
    greps reason strings out of the text-based describe / events dumps.
    Like the rest of this module, it never invents a verdict; it just
    surfaces the facts the agent needs to route the failure.
    """
    root = Path(artifact_dir)
    summary: dict[str, Any] = {
        "present": False,
        "pods_json_present": False,
        "events_present": False,
        "pod_count": 0,
        "pods": [],
        "phases": {},
        "container_states": {},
        "abnormal_reasons": [],
        "warning_events": [],
    }
    k8s_dir = root / "k8s"
    if not k8s_dir.is_dir():
        return summary

    files = sorted(p for p in k8s_dir.rglob("*") if p.is_file())
    if not files:
        return summary

    summary["present"] = True
    pods_json = k8s_dir / "pods.json"
    if pods_json.is_file():
        summary["pods_json_present"] = True
        try:
            data = json.loads(pods_json.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            data = None
        if isinstance(data, dict):
            items = data.get("items") or []
            if isinstance(items, list):
                summary["pod_count"] = len(items)
                for pod in items:
                    if not isinstance(pod, dict):
                        continue
                    meta = pod.get("metadata") or {}
                    status = pod.get("status") or {}
                    spec = pod.get("spec") or {}
                    phase = status.get("phase") or ""
                    if phase:
                        summary["phases"][phase] = summary["phases"].get(phase, 0) + 1

                    container_states: list[dict[str, Any]] = []
                    cstatuses = status.get("containerStatuses") or []
                    if isinstance(cstatuses, list):
                        for cs in cstatuses:
                            if not isinstance(cs, dict):
                                continue
                            state = cs.get("state") or {}
                            waiting = state.get("waiting") or {}
                            terminated = state.get("terminated") or {}
                            running = state.get("running") or {}
                            container_states.append(
                                {
                                    "name": cs.get("name"),
                                    "ready": bool(cs.get("ready")),
                                    "restartCount": int(cs.get("restartCount") or 0),
                                    "image": (cs.get("image") or ""),
                                    "current_state": (
                                        "waiting"
                                        if waiting
                                        else "terminated"
                                        if terminated
                                        else "running"
                                        if running
                                        else "unknown"
                                    ),
                                    "reason": (waiting.get("reason") or terminated.get("reason") or ""),
                                    "exitCode": terminated.get("exitCode"),
                                    "message": (waiting.get("message") or terminated.get("message") or ""),
                                }
                            )
                            reason = waiting.get("reason") or terminated.get("reason") or ""
                            if reason and _K8S_REASON_RX.search(reason):
                                summary["abnormal_reasons"].append(
                                    {
                                        "pod": meta.get("name"),
                                        "container": cs.get("name"),
                                        "reason": reason,
                                        "message": waiting.get("message") or terminated.get("message") or "",
                                    }
                                )

                    summary["pods"].append(
                        {
                            "name": meta.get("name"),
                            "phase": phase,
                            "nodeName": spec.get("nodeName"),
                            "podIP": status.get("podIP"),
                            "container_states": container_states,
                        }
                    )

                    for cs in container_states:
                        key = cs["current_state"]
                        if key:
                            summary["container_states"][key] = summary["container_states"].get(key, 0) + 1

    # Text-based events: surface warnings tied to this LWS.
    events_filtered = k8s_dir / "events.filtered.txt"
    events_all = k8s_dir / "events.all.txt"
    events_path = events_filtered if events_filtered.is_file() else events_all
    if events_path.is_file():
        summary["events_present"] = True
        try:
            for raw in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
                if "Warning" not in raw:
                    continue
                if _K8S_REASON_RX.search(raw) or "Failed" in raw or "BackOff" in raw:
                    summary["warning_events"].append(raw.strip()[:240])
        except OSError:
            pass
        if len(summary["warning_events"]) > 50:
            summary["warning_events"] = summary["warning_events"][:50]
            summary["warning_events_truncated"] = True

    if len(summary["abnormal_reasons"]) > 50:
        summary["abnormal_reasons"] = summary["abnormal_reasons"][:50]
        summary["abnormal_reasons_truncated"] = True
    if len(summary["pods"]) > 50:
        summary["pods"] = summary["pods"][:50]
        summary["pods_truncated"] = True

    return summary


# ---------------------------------------------------------------------------
# Cross-artifact text search
# ---------------------------------------------------------------------------

# Files we treat as "searchable text" inside the artifact bundle. We
# avoid tar members and binary blobs here; the agent can call the
# existing evidence API on individual files for deep dives.
_SEARCHABLE_TEXT_SUFFIXES = (".log", ".txt", ".json", ".yaml", ".yml", ".err", "")


def search_artifacts(
    artifact_dir: Path,
    pattern: str,
    *,
    max_matches: int = 50,
    max_files_scanned: int = 200,
) -> dict[str, Any]:
    """Search the artifact bundle for a regex.

    Used when the main job log is sparse (the multi-node workflow only
    streams leader stdout in the foreground; worker stdout is captured
    separately in ``collected-logs/node{i}/var/log``). The agent can
    request a single regex across K8s logs, plogs, pod stdout, and
    event files in one call.
    """
    root = Path(artifact_dir)
    out: dict[str, Any] = {
        "artifact_dir": str(root),
        "pattern": pattern,
        "matches": [],
        "files_scanned": 0,
    }
    if not root.exists() or not root.is_dir():
        return out
    try:
        rx = re.compile(pattern)
    except re.error as exc:
        out["error"] = str(exc)
        return out

    files_scanned = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if files_scanned >= max_files_scanned:
            out["files_scanned_truncated"] = True
            break
        rel = path.relative_to(root)
        if rel.parts[0] in {"ascend_logs"} and rel.suffix in {".tar", ".gz"}:
            continue
        if path.suffix.lower() not in _SEARCHABLE_TEXT_SUFFIXES:
            continue
        files_scanned += 1
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                for i, raw in enumerate(fh, start=1):
                    m = rx.search(raw)
                    if not m:
                        continue
                    out["matches"].append(
                        {
                            "file": str(rel),
                            "line": i,
                            "snippet": raw.strip()[:240],
                        }
                    )
                    if len(out["matches"]) >= max_matches:
                        out["matches_truncated"] = True
                        out["files_scanned"] = files_scanned
                        return out
        except OSError:
            continue

    out["files_scanned"] = files_scanned
    return out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _print_json(payload: Any) -> None:
    import sys

    sys.stdout.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    import argparse

    p = argparse.ArgumentParser(description="Parse CI diagnosis artifacts.")
    p.add_argument("--artifact-dir", type=Path, required=True)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("manifest")
    s = sub.add_parser("benchmark")
    s = sub.add_parser("k8s")
    s = sub.add_parser("search")
    s.add_argument("--pattern", required=True)
    s.add_argument("--max-matches", type=int, default=50)

    args = p.parse_args(argv)

    if args.cmd == "manifest":
        _print_json(build_manifest(args.artifact_dir))
    elif args.cmd == "benchmark":
        _print_json(summarize_benchmark_results(args.artifact_dir))
    elif args.cmd == "k8s":
        _print_json(summarize_k8s_state(args.artifact_dir))
    elif args.cmd == "search":
        _print_json(
            search_artifacts(
                args.artifact_dir,
                args.pattern,
                max_matches=args.max_matches,
            )
        )
    else:
        _print_json({"error": f"unknown cmd: {args.cmd!r}"})
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
