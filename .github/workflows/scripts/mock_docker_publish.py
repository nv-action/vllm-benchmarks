#!/usr/bin/env python3
"""Stand in for Docker manifest publication during the full workflow mock."""

import hashlib
import json
import os
import sys
from pathlib import Path


def main() -> None:
    args = sys.argv[1:]
    if args[:3] == ["buildx", "imagetools", "inspect"]:
        refs_dir = Path(os.environ["RUNNER_TEMP"]) / "mock-image-refs"
        image_ref = args[3]
        if any(path.read_text(encoding="utf-8").strip() == image_ref for path in refs_dir.rglob("ref.txt")):
            return
        raise SystemExit(f"Simulated image was not published: {image_ref}")
    if args[:3] != ["buildx", "imagetools", "create"]:
        raise SystemExit(f"Unexpected simulated Docker command: {args}")
    if "--metadata-file" not in args:
        raise SystemExit("Simulated manifest creation requires --metadata-file")
    metadata = Path(args[args.index("--metadata-file") + 1])
    sources = []
    index = 3
    while index < len(args):
        if args[index] in ("-t", "--metadata-file"):
            index += 2
        else:
            sources.append(args[index])
            index += 1
    if len(sources) != 2:
        raise SystemExit(f"Expected two architecture sources, got {sources}")
    digest = "sha256:" + hashlib.sha256("\n".join(sources).encode()).hexdigest()
    metadata.parent.mkdir(parents=True, exist_ok=True)
    metadata.write_text(json.dumps({"containerimage.descriptor": {"digest": digest}}), encoding="utf-8")


if __name__ == "__main__":
    main()
