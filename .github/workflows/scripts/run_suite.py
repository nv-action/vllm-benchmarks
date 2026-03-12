#!/usr/bin/env python3
#
# Copyright (c) 2025 Huawei Technologies Co., Ltd. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# This file is a part of the vllm-ascend project.
#
"""
Run test suites for vLLM Ascend E2E tests.

This script runs test suites with support for:
- Parallel test execution with auto-partitioning
- Timing data collection and upload
- Continue-on-error mode for CI

Usage:
    python3 .github/workflows/scripts/run_suite.py \\
        --suite e2e-singlecard \\
        --auto-partition-id 0 \\
        --auto-partition-size 2 \\
        --auto-upgrade-estimated-times \\
        --continue-on-error
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


# Test suite definitions
SUITES: dict[str, dict[str, Any]] = {
    "e2e-singlecard": {
        "base_dir": "tests/e2e/singlecard",
        "tests": [
            "test_models.py",
            "test_prefix_caching.py",
            "test_chunked_prefill.py",
            "test_verify_generation.py",
            "test_verify_outputs.py",
            "test_multimodal.py",
        ],
    },
    "e2e-singlecard-light": {
        "base_dir": "tests/e2e/singlecard",
        "tests": [
            "test_models.py",
        ],
    },
    "e2e-2card-light": {
        "base_dir": "tests/e2e/multicard/2-cards",
        "tests": [
            "test_models.py",
        ],
    },
    "e2e-multicard-2-cards": {
        "base_dir": "tests/e2e/multicard/2-cards",
        "tests": [
            "test_models.py",
            "test_moe.py",
            "test_pp_tp.py",
            "test_prefix_caching.py",
            "test_verify_generation.py",
        ],
    },
    "e2e-multicard-4-cards": {
        "base_dir": "tests/e2e/multicard/4-cards",
        "tests": [
            "test_models.py",
            "test_moe.py",
            "test_pp_tp.py",
        ],
    },
}


def parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Run test suites for vLLM Ascend E2E tests"
    )
    parser.add_argument(
        "--suite",
        required=True,
        choices=list(SUITES.keys()),
        help="Test suite to run",
    )
    parser.add_argument(
        "--auto-partition-id",
        type=int,
        default=0,
        help="Partition ID for parallel execution",
    )
    parser.add_argument(
        "--auto-partition-size",
        type=int,
        default=1,
        help="Total number of partitions",
    )
    parser.add_argument(
        "--auto-upgrade-estimated-times",
        action="store_true",
        help="Collect and output timing data",
    )
    parser.add_argument(
        "--continue-on-error",
        action="store_true",
        help="Continue running tests even if some fail",
    )
    return parser.parse_args()


def partition_tests(
    tests: list[str],
    partition_id: int,
    partition_size: int,
) -> list[str]:
    """Partition tests for parallel execution."""
    if partition_size <= 1:
        return tests

    # Simple round-robin partitioning
    return [test for i, test in enumerate(tests) if i % partition_size == partition_id]


def run_test(
    test_path: str,
    continue_on_error: bool,
    timing_data: list[dict[str, Any]],
) -> bool:
    """Run a single test file and collect timing data."""
    start_time = time.time()
    passed = False

    try:
        cmd = ["pytest", "-sv", "--durations=0", test_path]
        print(f"\n{'='*60}")
        print(f"Running: {' '.join(cmd)}")
        print(f"{'='*60}\n")

        result = subprocess.run(
            cmd,
            check=False,
            text=True,
        )

        passed = result.returncode == 0

        if passed:
            print(f"✓ PASSED: {test_path}")
        else:
            print(f"✗ FAILED: {test_path} returned exit code {result.returncode}")

    except Exception as e:
        print(f"✗ FAILED: {test_path} - {e}")
        passed = False

    elapsed = time.time() - start_time

    # Collect timing data
    timing_data.append({
        "name": test_path,
        "elapsed": elapsed,
        "passed": passed,
    })

    return passed


def main() -> int:
    """Main entry point."""
    args = parse_args()

    # Get suite configuration
    suite_config = SUITES[args.suite]
    base_dir = suite_config["base_dir"]
    all_tests = suite_config["tests"]

    # Partition tests if needed
    tests = partition_tests(
        all_tests,
        args.auto_partition_id,
        args.auto_partition_size,
    )

    if not tests:
        print(f"No tests to run for partition {args.auto_partition_id}/{args.auto_partition_size}")
        return 0

    print(f"\nTest Suite: {args.suite}")
    print(f"Partition: {args.auto_partition_id + 1}/{args.auto_partition_size}")
    print(f"Tests to run: {len(tests)}")
    print(f"Test files: {tests}\n")

    # Run tests
    timing_data: list[dict[str, Any]] = []
    all_passed = True

    for test_file in tests:
        test_path = str(Path(base_dir) / test_file)
        passed = run_test(test_path, args.continue_on_error, timing_data)

        if not passed:
            all_passed = False
            if not args.continue_on_error:
                print("\nTest failed. Exiting.")
                break

    # Output timing data if requested
    if args.auto_upgrade_estimated_times and timing_data:
        output_file = Path("test_timing_data.json")
        output_file.write_text(
            json.dumps(
                {
                    "suite": args.suite,
                    "partition": args.auto_partition_id,
                    "tests": timing_data,
                },
                indent=2,
            )
        )
        print(f"\nTiming data written to {output_file}")

    # Print summary
    print(f"\n{'='*60}")
    print("Test Summary:")
    print(f"{'='*60}")
    for test in timing_data:
        status = "✓ PASSED" if test["passed"] else "✗ FAILED"
        print(f"{status}: {test['name']} ({test['elapsed']:.2f}s)")

    if all_passed:
        print("\n✓ All tests passed!")
        return 0
    else:
        print("\n✗ Some tests failed.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
