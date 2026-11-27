"""Replay PR 23444 with the original parser. Only format diagnostics reach Actions."""

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import regex as re


def load_review(workspace):
    path = workspace / ".github/workflows/scripts/ai_review.py"
    spec = importlib.util.spec_from_file_location("production_review", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def build_request(workspace, review):
    base = review.git(workspace / "base", "-c", f"safe.directory={workspace / 'base'}", "rev-parse", "HEAD").strip()
    head = review.git(workspace / "head", "-c", f"safe.directory={workspace / 'head'}", "rev-parse", "HEAD").strip()
    if (base, head) != (
        "ebaaeeec5b50a5769cf665666072a0db62df8645",
        "fc1d0e9426854305fe974e2b0fcee4dc061e368c",
    ):
        raise ValueError("Unexpected checkout")
    diff = review.git(workspace, "diff", "--no-ext-diff", "--no-textconv", base, head)
    files = review.git(workspace, "diff", "--name-only", "-z", base, head).rstrip("\0").split("\0")
    registry = (workspace / ".github/agent-review/domains.yaml").read_text()
    return {
        "state": {"head": head, "findings": []},
        "merge_base": base,
        "domains": [domain["name"] for domain in review.resolve_domains(registry, files)],
        "files": files,
        "diff": diff[: review.MAX_DIFF_CHARS],
        "truncated": len(diff) > review.MAX_DIFF_CHARS,
        "reply": "",
    }


def first_token(text):
    value = text.lstrip()
    if not value:
        return "whitespace" if text else "empty"
    for prefix, name in (
        ("\ufeff", "BOM"),
        ("\x1b", "ESC"),
        ("```json", "markdown-json-fence"),
        ("```", "markdown-fence"),
        ("<think>", "think-tag"),
        ("{", "object-opening-brace"),
        ("[", "array-opening-bracket"),
        ('"', "double-quote"),
    ):
        if value.startswith(prefix):
            return name
    if value[0].isalpha():
        return "letter"
    if value[0].isdigit():
        return "digit"
    return "other-punctuation-or-control"


def analyze(output, parse):
    report = {"output_bytes": len(output.encode()), "first_token": first_token(output)}
    # Only whitespace, punctuation and fixed format markers are public.
    shape = re.sub(r"[^\s\x1b\[\]{}()<>`~.,!?:;/#|=_+-]", "x", output[:96])
    report["prefix_shape"] = re.sub("x+", "x", shape)
    report["line_count"] = len(output.splitlines())
    report["ends_with_fence"] = output.rstrip().endswith("```")
    try:
        result = parse(output, [])
    except json.JSONDecodeError as error:
        report.update(status="invalid-json", error_line=error.lineno, error_column=error.colno, error_offset=error.pos)
    except ValueError:
        report.update(status="schema-rejected", json_type=type(json.loads(output)).__name__)
    else:
        report.update(
            status="valid-review", findings_count=len(result["findings"]), updates_count=len(result["updates"])
        )
        return report

    text = output.strip()
    fence = re.fullmatch(r"```(?:json)?[^\S\r\n]*\r?\n(.*?)\r?\n```", text, flags=re.IGNORECASE | re.DOTALL)
    if fence:
        try:
            parse(fence[1], [])
        except ValueError:
            report["valid_after_stripping_fence"] = False
        else:
            report["valid_after_stripping_fence"] = True

    decoder = json.JSONDecoder()
    offset = 0
    values = 0
    while offset < len(text):
        try:
            _, end = decoder.raw_decode(text, offset)
        except ValueError:
            break
        values += 1
        offset = end
        while offset < len(text) and text[offset].isspace():
            offset += 1
    report["multiple_top_level_json_values"] = values > 1 and offset == len(text)

    for opening in re.finditer(r"\{", text):
        start = opening.start()
        try:
            _, end = decoder.raw_decode(text, start)
            parse(text[start:end], [])
        except ValueError:
            continue
        report.update(contains_valid_review=True, prefix_chars=start, suffix_chars=len(text) - end)
        break
    else:
        report["contains_valid_review"] = False
    return report


def main(workspace):
    review = load_review(workspace)
    request = build_request(workspace, review)
    prompt = review.build_prompt(workspace, request)
    print(
        json.dumps({"prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(), "diff_chars": len(request["diff"])}),
        flush=True,
    )
    # The headless CLI streams provider reasoning to the inherited descriptor.
    # Capture it privately; only the presence of a known progress marker is logged.
    with tempfile.TemporaryFile() as private_log:
        saved_stderr = os.dup(2)
        try:
            os.dup2(private_log.fileno(), 2)
            output = review.run_agent(workspace, request)
        except subprocess.TimeoutExpired:
            print(json.dumps({"status": "agent-timeout"}))
            return 3
        except subprocess.CalledProcessError as error:
            print(json.dumps({"status": "agent-exit", "exit_code": error.returncode}))
            return 3
        finally:
            os.dup2(saved_stderr, 2)
            os.close(saved_stderr)
        private_log.seek(0)
        reasoning_seen = any(b"dsh: reasoning:" in line for line in private_log)

    report = analyze(output, review.parse_result)
    report["reasoning_seen_on_stderr"] = reasoning_seen
    print(json.dumps(report, sort_keys=True))
    return 0 if report["status"] == "valid-review" else 2


if __name__ == "__main__":
    try:
        status = main(Path(sys.argv[1]).resolve())
    except Exception as error:
        print(json.dumps({"status": "replay-error", "error_type": type(error).__name__}))
        status = 4
    sys.exit(status)
