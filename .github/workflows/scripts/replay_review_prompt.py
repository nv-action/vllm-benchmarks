"""Exercise the candidate production prompt and parser. Print only aggregate diagnostics."""

import importlib.util
import json
import subprocess
import sys
import tempfile
import time
from pathlib import Path

BASE = "ebaaeeec5b50a5769cf665666072a0db62df8645"
HEAD = "fc1d0e9426854305fe974e2b0fcee4dc061e368c"


def load_review(workspace):
    spec = importlib.util.spec_from_file_location(
        "candidate_review", workspace / ".github/workflows/scripts/ai_review.py"
    )
    review = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = review
    spec.loader.exec_module(review)
    return review


def git_diagnostics(trace):
    counts = {"patch_diff_calls": 0, "show_calls": 0}
    if trace.exists():
        for line in trace.read_text().splitlines():
            event = json.loads(line)
            if event.get("event") == "start":
                args = event.get("argv", [])
                if "diff" in args and not any(
                    arg
                    in (
                        "--stat",
                        "--shortstat",
                        "--name-only",
                        "--name-status",
                        "--numstat",
                        "--check",
                        "--quiet",
                        "--no-patch",
                        "-s",
                    )
                    for arg in args
                ):
                    counts["patch_diff_calls"] += 1
                elif "show" in args:
                    counts["show_calls"] += 1
    return counts


def replay(workspace):
    review = load_review(workspace)
    kb = workspace / ".github/agent-review"
    assert review.git(workspace / "head", "rev-parse", "HEAD").strip() == HEAD
    assert review.git(workspace, "merge-base", BASE, HEAD).strip() == BASE
    files = review.git(workspace, "diff", "--name-only", "-z", BASE, HEAD).rstrip("\0").split("\0")
    registry = (kb / "domains.yaml").read_text()
    pr = json.loads(Path("/tmp/replay-public-pr.json").read_text())
    request = {
        "title": pr["title"],
        "body": pr["body"] or "",
        "merge_base": BASE,
        "state": {"base": BASE, "head": HEAD, "findings": []},
        "domains": [domain["name"] for domain in review.resolve_domains(registry, files)],
        "diff_stat": review.git(workspace, "diff", "--stat", "--no-ext-diff", "--no-textconv", BASE, HEAD).rstrip("\n"),
        "reply": "",
    }
    report = {
        "changed_paths": len(files),
        "pinned_rules_loaded": (kb / "L2/doc-quality/upstream").is_dir(),
    }
    with tempfile.TemporaryDirectory() as tempdir:
        trace = Path(tempdir) / "git-events.jsonl"
        subprocess.run(["git", "config", "--global", "trace2.eventTarget", str(trace)], check=True)
        started = time.monotonic()
        try:
            out = review.run_agent(workspace, request)
            report["output_bytes"] = len(out.encode())
            parsed = review.parse_result(out, [])
        except subprocess.TimeoutExpired:
            report["status"] = "agent-timeout"
        except subprocess.CalledProcessError as error:
            report.update(status="agent-exit", exit_code=error.returncode)
        except json.JSONDecodeError as error:
            report.update(status="invalid-json", offset=error.pos)
        except ValueError:
            report["status"] = "schema-rejected"
        else:
            findings = parsed["findings"]
            report.update(
                status="valid-review",
                findings=len(findings),
                updates=len(parsed["updates"]),
                findings_on_changed_paths=sum(f["path"] in files for f in findings),
                coverage_complete=parsed["coverage_complete"],
                requires_human=parsed["requires_human"],
            )
        finally:
            report["agent_seconds"] = round(time.monotonic() - started, 1)
            subprocess.run(["git", "config", "--global", "--unset", "trace2.eventTarget"], check=True)
        report.update(git_diagnostics(trace))
    return report


if __name__ == "__main__":
    try:
        result = replay(Path(sys.argv[1]).resolve())
    except Exception as error:
        result = {"status": "replay-error", "error_type": type(error).__name__}
    print(json.dumps(result, sort_keys=True), flush=True)
    sys.exit(0 if result["status"] == "valid-review" else 1)
