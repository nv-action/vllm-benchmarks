#!/usr/bin/env python3
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved
#
# Licensed under the Apache License, Version 2.0 (the "License")
#
# you may not use this file except in compliance with the License
#
# You may obtain a copy of the License at
#
# <http://www.apache.org/licenses/LICENSE-2.0>
#
# Unless required by applicable law or agreed to in writing, software
#
# distributed under the License is distributed on an "AS IS" BASIS
#
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied
#
# See the License for the specific language governing permissions and
#
# limitations under the License
#
# This file is a part of the vllm-ascend project
#

"""Generate a PR comment markdown from a diff-cover coverage report.

diff-cover produces a per-file markdown table in diff_coverage.md.
This script extracts the overall diff coverage ratio from that report
and formats a compact PR comment that the post_fork_coverage_comment
workflow can post directly to the PR.
"""

import argparse
import re
import sys
from pathlib import Path


def extract_diff_coverage(markdown: str) -> dict:
    """Parse diff-cover's markdown output.

    diff-cover v7+ emits a line like:
        Overall coverage for lines changed: 89.56%

    Older versions use a table with columns:
        File | Modified | Uncovered | Coverage %

    Returns a dict with keys: overall, files (list of dicts).
    """
    result = {"overall": None, "files": []}

    # Try the new-style single-line summary first.
    m = re.search(
        r"Overall coverage for lines changed:\s*([\d.]+)%",
        markdown,
    )
    if m:
        result["overall"] = float(m.group(1))
        return result

    # Fall back to parsing the old-style file table.
    for line in markdown.splitlines():
        line = line.strip()
        # A table row looks like:  | vllm_ascend/foo.py | 15 | 3 | 80.0%  |
        parts = [p.strip() for p in line.split("|") if p.strip()]
        if len(parts) >= 4 and parts[0].endswith(".py"):
            try:
                modified = int(parts[1])
                uncovered = int(parts[2])
                pct = float(parts[3].rstrip("%"))
            except (ValueError, IndexError):
                continue
            result["files"].append(
                {"file": parts[0], "modified": modified,
                 "uncovered": uncovered, "pct": pct}
            )

    if result["files"]:
        total_mod = sum(f["modified"] for f in result["files"])
        total_unc = sum(f["uncovered"] for f in result["files"])
        if total_mod:
            result["overall"] = round((1 - total_unc / total_mod) * 100, 2)

    return result


def build_comment(cover: dict) -> str:
    """Render a compact markdown PR comment from parsed coverage."""
    lines = ["## Diff Coverage\n"]

    if cover["overall"] is not None:
        overall = f"**{cover['overall']:.2f}%**"
        lines.append(f"Overall coverage for changed lines: {overall}\n")
    else:
        lines.append("No diff coverage data found.\n")
        return "\n".join(lines)

    if cover["files"]:
        lines.extend(["| File | Modified | Uncovered | Coverage % |",
                       "|------|----------|-----------|------------|"])
        for f in cover["files"]:
            flag = " :x:" if f["uncovered"] else ""
            lines.append(
                f"| {f['file']} | {f['modified']} "
                f"| {f['uncovered']}{flag} | {f['pct']:.1f}% |"
            )
        lines.append("")

    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True,
                        help="Path to diff_coverage.md")
    parser.add_argument("--output", required=True,
                        help="Path to write coverage_comment.md")
    args = parser.parse_args()

    in_path = Path(args.input)
    if not in_path.exists():
        print(f"::warning::input file {in_path} not found; skipping",
              file=sys.stderr)
        return 0

    markdown = in_path.read_text(encoding="utf-8")
    cover = extract_diff_coverage(markdown)
    comment = build_comment(cover)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(comment, encoding="utf-8")
    print(f"Wrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
