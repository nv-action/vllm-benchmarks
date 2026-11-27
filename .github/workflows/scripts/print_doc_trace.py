"""Relay the public part of dsh reasoning while retaining original stderr privately."""

import json
import os
import re
import sys
from pathlib import Path

WINDOW = 20
MARK = "[REDACTED]"
SECRET_NAMES = ("AI_REVIEW_KEY", "AI_REVIEW_API", "DEEPSEEK_API_KEY", "DOC_QUALITY_TOKEN", "GH_TOKEN", "GITHUB_TOKEN")


def string_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, dict)):
        for item in value.values() if isinstance(value, dict) else value:
            yield from string_values(item)


def build_index(directory):
    needles = {WINDOW: set()}
    for path in directory.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in {".md", ".json", ".txt", ".yaml", ".yml", ".csv"}:
            continue
        text = path.read_text(encoding="utf-8-sig")
        entries = [text]
        if path.suffix.lower() == ".json":
            entries.extend(string_values(json.loads(text)))
        for entry in entries:
            flat = re.sub(r"\s+", " ", entry)
            needles[WINDOW].update(flat[i : i + WINDOW] for i in range(len(flat) - WINDOW + 1))
            for phrase in re.split(r"[\r\n。！？；;]+", entry):
                short = phrase.strip(' \t#|*-[],:"`')
                if len(short) >= WINDOW or not any(c.isalpha() for c in short):
                    continue
                if len(short) >= 10 or sum("\u3400" <= c <= "\u9fff" for c in short) >= 4:
                    needles.setdefault(len(short), set()).add(short)
    return needles


def scrub(line, index, secrets):
    for secret in secrets:
        line = line.replace(secret, MARK)
    # Match whitespace-normalized text while preserving untouched original characters.
    parts = list(re.finditer(r"\s+|\S", line))
    flat = "".join(" " if p.group().isspace() else p.group() for p in parts)
    spans = []
    for i in range(len(flat)):
        for size, needles in index.items():
            if i + size <= len(flat) and flat[i : i + size] in needles:
                start, end = parts[i].start(), parts[i + size - 1].end()
                if spans and spans[-1][1] >= start:
                    spans[-1] = (spans[-1][0], max(spans[-1][1], end))
                else:
                    spans.append((start, end))
    for start, end in reversed(spans):
        while start < end and line[start].isspace():
            start += 1
        while end > start and line[end - 1].isspace():
            end -= 1
        line = line[:start] + MARK + line[end:]
    return line


def main():
    rules, target = map(Path, sys.argv[1:])
    index = build_index(rules)
    secrets = sorted({os.environ[k] for k in SECRET_NAMES if os.environ.get(k)}, key=len, reverse=True)
    reasoning = False
    fd = os.open(target, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "wb") as raw:
        os.fchmod(fd, 0o600)
        for wire in sys.stdin.buffer:
            raw.write(wire)
            raw.flush()
            text = wire.decode("utf-8", errors="replace").rstrip("\r\n")
            if text.startswith("dsh:") or text.startswith("Traceback (most recent call last):"):
                reasoning = text == "dsh: reasoning:"
            if reasoning:
                print(scrub(text, index, secrets), flush=True)


if __name__ == "__main__":
    main()
