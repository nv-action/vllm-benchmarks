# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

from e2e_image import plan_groups


def test_pilot_keeps_all_a2_a3_partitions_and_ignores_other_hardware():
    groups = [
        {"npu_type": "cpu"},
        {"npu_type": "a2", "tests": "one.py"},
        {"npu_type": "a3", "tests": "two.py"},
        {"npu_type": "a3", "tests": "four.py"},
        {"npu_type": "310p"},
    ]
    original = copy.deepcopy(groups)
    matrix = plan_groups(groups)
    assert [item["target"]["device"] for item in matrix] == ["a2", "a3"]
    assert matrix[0]["groups"] == groups[1:2]
    assert matrix[1]["groups"] == groups[2:4]
    assert groups == original


def test_no_empty_hardware_jobs():
    groups = [{"npu_type": "a3", "tests": "two.py"}]
    matrix = plan_groups(groups)
    assert len(matrix) == 1
    assert matrix[0]["target"]["device"] == "a3"
    assert matrix[0]["groups"] == groups
    assert plan_groups([]) == []


def test_complete_configured_suite_reaches_the_image_matrix(tmp_path):
    output = tmp_path / "outputs"
    subprocess.run(
        [sys.executable, str(Path(__file__).with_name("select_tests.py")), "--all-tests", "--skip-cpu-ut"],
        env={**os.environ, "GITHUB_OUTPUT": str(output)},
        check=True,
        capture_output=True,
        text=True,
    )
    values = dict(line.split("=", 1) for line in output.read_text().splitlines())
    groups = json.loads(values["test_groups"])
    matrix = plan_groups(groups)
    assert {item["target"]["device"] for item in matrix} == {"a2", "a3"}
    for item in matrix:
        expected = [group for group in groups if group["npu_type"] == item["target"]["device"]]
        assert item["groups"] == expected
        assert all(group["tests"] for group in expected)
