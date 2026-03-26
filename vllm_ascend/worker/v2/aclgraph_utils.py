# Adapt from https://github.com/vllm-project/vllm/blob/main/vllm/v1/worker/gpu/cudagraph_utils.py
# SPDX-License-Identifier: Apache-2.0
# SPDX-FileCopyrightText: Copyright contributors to the vLLM project
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
from contextlib import contextmanager
from typing import Any

import torch
import torch.nn as nn
from vllm.config import VllmConfig
from vllm.config.compilation import CUDAGraphMode
from vllm.v1.attention.backend import AttentionMetadataBuilder
from vllm.v1.kv_cache_interface import KVCacheConfig
from vllm.v1.worker.gpu.block_table import BlockTables
from vllm.v1.worker.gpu.cudagraph_utils import CudaGraphManager
from vllm.v1.worker.gpu.input_batch import InputBuffers

from vllm_ascend.worker.v2.utils import torch_cuda_wrapper


class AclGraphManager(CudaGraphManager):
    """ACL Graph Manager for Ascend NPUs."""

    def __init__(
        self,
        vllm_config: VllmConfig,
        device: torch.device,
    ):
        # Get cudagraph_mode and decode_query_len from config
        cudagraph_mode = vllm_config.compilation_config.cudagraph_mode
        # Calculate decode_query_len similar to GPUModelRunner
        decode_query_len = 1
        spec_config = vllm_config.speculative_config
        if spec_config is not None:
            decode_query_len += spec_config.num_speculative_tokens

        with torch_cuda_wrapper():
            super().__init__(vllm_config, device, cudagraph_mode, decode_query_len)

    # Note: The capture_graph method has been removed in the new vLLM API.
    # The parent CudaGraphManager now uses a different capture mechanism
    # via the capture() method that takes a create_forward_fn factory.
    # If NPU-specific capture logic is needed, override the capture() method
    # or provide a custom create_forward_fn to the parent's capture() method.
