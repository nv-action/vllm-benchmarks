#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

# Dedicated image-pilot runner: no environment installation or PR coverage flow.
# Run every assigned file in its own process group and preserve all outcomes.
set -euo pipefail
if [[ $# -lt 2 ]]; then
    echo "Usage: $0 <output-directory> <pytest-target> [pytest-target ...]" >&2
    exit 2
fi
output_dir=$1
shift
mkdir -p "$output_dir"
output_dir=$(cd "$output_dir" && pwd)
export VLLM_CACHE_ROOT TORCH_EXTENSIONS_DIR
VLLM_CACHE_ROOT=$(mktemp -d "${RUNNER_TEMP:-/tmp}/e2e-vllm-cache.XXXXXX")
TORCH_EXTENSIONS_DIR=$(mktemp -d "${RUNNER_TEMP:-/tmp}/e2e-torch-extensions.XXXXXX")
test_pid=""
tail_pid=""

cleanup_test() {
    if [[ -n "$test_pid" ]]; then
        kill -TERM -- "-$test_pid" 2>/dev/null || true
        for _ in {1..10}; do
            if ! kill -0 -- "-$test_pid" 2>/dev/null; then break; fi
            sleep 0.5
        done
        kill -KILL -- "-$test_pid" 2>/dev/null || true
    fi
    if [[ -n "$tail_pid" ]]; then
        kill "$tail_pid" 2>/dev/null || true
        wait "$tail_pid" 2>/dev/null || true
    fi
    test_pid=""
    tail_pid=""
}
trap cleanup_test EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

overall_status=0
index=0
: > "$output_dir/results.tsv"
for target in "$@"; do
    index=$((index + 1))
    log_file="$output_dir/$index.log"
    start_time=$(date +%s)
    echo "::group::$target"
    setsid python3 -m pytest -sv --color=yes --junitxml="$output_dir/$index.xml" "$target" > "$log_file" 2>&1 &
    test_pid=$!
    tail --pid="$test_pid" -n +1 -f "$log_file" &
    tail_pid=$!
    status=0
    wait "$test_pid" || status=$?
    # Allow tail to flush the final output before cleaning leaked workers.
    wait "$tail_pid" || true
    cleanup_test
    elapsed=$(( $(date +%s) - start_time ))
    printf '%s\t%s\t%s\t%s\n' "$target" "$status" "$elapsed" "$log_file" >> "$output_dir/results.tsv"
    echo "::endgroup::"
    if [[ "$status" -ne 0 ]]; then overall_status=1; fi
done
python3 - "$output_dir" <<'PY'
import csv
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
with (root / "results.tsv").open() as stream:
    results = [
        {"test": test, "exit_code": int(code), "seconds": int(seconds), "log": log}
        for test, code, seconds, log in csv.reader(stream, delimiter="\t")
    ]
(root / "results.json").write_text(json.dumps(results, indent=2) + "\n")
print(json.dumps(results, indent=2))
PY
exit "$overall_status"
