#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

"""Group the complete A2/A3 E2E partitions by the image they consume."""

import argparse
import json
import os
from pathlib import Path

from resolve_csrc_cache_targets import resolve_targets

PILOT_TARGETS = ("a2-arm64-ubuntu", "a3-arm64-ubuntu")


def plan_groups(groups: list[dict]) -> list[dict]:
    targets = resolve_targets(
        list(PILOT_TARGETS), config_path=Path(".github/workflows/scripts/csrc_cache_targets.json")
    )
    matrix = []
    for target in targets:
        partitions = [group for group in groups if group["npu_type"] == target["device"]]
        if partitions:
            matrix.append({"target": target, "groups": partitions})
    return matrix


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--groups", required=True)
    args = parser.parse_args()
    matrix = plan_groups(json.loads(args.groups))
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
        print(f"image_matrix={json.dumps(matrix, separators=(',', ':'))}", file=output)


if __name__ == "__main__":
    main()
