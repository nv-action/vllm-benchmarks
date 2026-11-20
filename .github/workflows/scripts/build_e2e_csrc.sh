#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Huawei Technologies Co., Ltd. All Rights Reserved.

# Run from the source checkout; the workflow has already restored OBS caches.
# Usage: build_e2e_csrc.sh <on|off> <l0-hit> <l0-dir> <l1-seed> <local-l1-dir>
set -euo pipefail

cache_mode=$1
l0_hit=$2
l0_dir=$3
l1_seed=$4
local_l1_dir=$5

if [[ "$cache_mode" == on && "$l0_hit" == true ]]; then
    echo "E2E csrc: L0 hit, skipping native compilation"
    cp -a "$l0_dir/." vllm_ascend/
    COMPILE_CUSTOM_KERNELS=0 uv pip install --no-deps -e . --no-build-isolation
else
    export VLLM_ASCEND_BUILD_CACHE_DIR
    if [[ "$cache_mode" == on ]]; then
        VLLM_ASCEND_BUILD_CACHE_DIR=$local_l1_dir
        mkdir -p "$local_l1_dir"
        # Merge the OBS snapshot with local entries; the engine checks cached actions.
        cp -a "$l1_seed/." "$local_l1_dir/"
        echo "E2E csrc: source build with L1 cache"
    else
        # The writable seed mount discards these fresh entries after the RUN.
        VLLM_ASCEND_BUILD_CACHE_DIR=$(mktemp -d "$l1_seed/XXXXXX")
        echo "E2E csrc: source build without restored caches"
    fi
    COMPILE_CUSTOM_KERNELS=1 uv pip install --no-deps -e . --no-build-isolation
fi
