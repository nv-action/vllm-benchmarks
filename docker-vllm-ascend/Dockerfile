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
# CANN_QUAY_URL (optional): Registry URL of the CANN base image.
ARG CANN_QUAY_URL="quay.io/ascend/cann"
# CANN_VERSION (optional): CANN toolkit version used to select the base image tag.
ARG CANN_VERSION="9.1.0"

FROM ${CANN_QUAY_URL}:${CANN_VERSION}-910b-ubuntu22.04-py3.12

# 压测用 cache-bust 标记：每个 job 传入唯一值，强制下面的 apt/pip/编译层全部重跑，
# 制造真实冷构建（否则同组合的 load 副本会命中 buildkitd layer cache，几秒就结束）。
ARG CACHE_BUSTER=0
RUN echo "cache-buster=${CACHE_BUSTER}"

# Build-time arguments (declared once at the top of the stage for clarity).
# PYTORCH_INDEX_URL (optional): Extra pip index URL for PyTorch wheels.
ARG PYTORCH_INDEX_URL="https://download.pytorch.org/whl/cpu/"
# ASCEND_INDEX_URL (optional): Extra pip index URL for Ascend/CANN Python packages.
ARG ASCEND_INDEX_URL="https://mirrors.huaweicloud.com/ascend/repos/pypi"
# PIP_INDEX_URL: primary pip index -> huaweicloud mirror. pypi.org via squid
# intermittently returns "No matching distribution" under 64-way burst; the
# mirror is fast and reliable from the cluster. Retries/timeout ride out
# transient index hiccups.
ENV PIP_INDEX_URL="https://repo.huaweicloud.com/repository/pypi/simple" \
    PIP_RETRIES=10 \
    PIP_TIMEOUT=60
# MOONCAKE_TAG (optional): Version tag of the mooncake-transfer-engine-npu package.
ARG MOONCAKE_TAG=0.3.11.post1
# VLLM_REPO (optional): Git repository URL of vLLM.
ARG VLLM_REPO=https://github.com/vllm-project/vllm.git
# VLLM_TAG (optional): vLLM release tag to clone when VLLM_COMMIT is empty.
ARG VLLM_TAG=v0.30.0
# VLLM_COMMIT (optional): Exact vLLM commit to build; empty to fall back to VLLM_TAG.
ARG VLLM_COMMIT=""
# SOC_VERSION (optional): Ascend SoC version used for custom kernel compilation.
ARG SOC_VERSION="ascend910b1"
# COMPILE_CUSTOM_KERNELS (optional): Whether to compile custom Ascend kernels (1 = yes, 0 = no).
ARG COMPILE_CUSTOM_KERNELS=1
# BUILD_TYPE (optional): Build type: 'release' or 'daily' (daily installs extra deps).
ARG BUILD_TYPE="release"
# MEMCACHE_VERSION (optional, empty if unset): memcache package version for daily builds.
ARG MEMCACHE_VERSION
# MEMCACHE_DATE (optional, empty if unset): memcache package build date for daily builds.
ARG MEMCACHE_DATE
# MEMFABRIC_VERSION (optional, empty if unset): memfabric package version for daily builds.
ARG MEMFABRIC_VERSION
# MEMFABRIC_DATE (optional, empty if unset): memfabric package build date for daily builds.
ARG MEMFABRIC_DATE
# TORCH_NPU_VERSION (optional, empty if unset): torch_npu version for daily builds.
ARG TORCH_NPU_VERSION
# TORCH_NPU_DATE (optional, empty if unset): torch_npu build date for daily builds.
ARG TORCH_NPU_DATE
# TRITON_ASCEND_VERSION (optional, empty if unset): triton-ascend version for daily builds.
ARG TRITON_ASCEND_VERSION
# TRITON_ASCEND_PACKAGE_VERSION (optional, empty if unset): triton-ascend package version for daily builds.
ARG TRITON_ASCEND_PACKAGE_VERSION
# DAILY_DEPS_MODE (optional): Daily deps install mode: 'full' or 'torch_npu_only'.
ARG DAILY_DEPS_MODE="full"
# flash-linear-attention-npu
ARG FLA_VERSION="26.9.1"
ARG FLA_RELEASE_TAG="v26.9.1-release"

WORKDIR /workspace

# Install clang-15 (for triton-ascend) and Mooncake
RUN apt-get update -y && \
    apt-get install -y git vim wget curl protobuf-compiler libprotobuf-dev net-tools gcc g++ cmake numactl libnuma-dev libibverbs-dev libhiredis-dev clang-15 && \
    update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 20 && \
    update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-15 20 && \
    source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    python3 -m pip install mooncake-transfer-engine-npu==${MOONCAKE_TAG} && \
    rm -rf /var/cache/apt/* && \
    rm -rf /var/lib/apt/lists/*

# Install modelscope (for fast download) and ray (for multinode)
RUN --mount=type=cache,target=/root/.cache/pip python3 -m pip install 'modelscope<1.38' 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0'

# Install vLLM
RUN if [ -n "$VLLM_COMMIT" ]; then \
      git init /vllm-workspace/vllm && \
      git -C /vllm-workspace/vllm fetch --depth 1 $VLLM_REPO "$VLLM_COMMIT" && \
      git -C /vllm-workspace/vllm checkout FETCH_HEAD; \
    else \
      git clone --depth 1 -b $VLLM_TAG $VLLM_REPO /vllm-workspace/vllm; \
    fi

# In x86, triton will be installed by vllm. But in Ascend, triton doesn't work correctly. we need to uninstall it.
RUN --mount=type=cache,target=/root/.cache/pip VLLM_TARGET_DEVICE="empty" python3 -m pip install -e /vllm-workspace/vllm/[audio] --extra-index-url ${PYTORCH_INDEX_URL} && \
    python3 -m pip uninstall -y triton

# Install vllm-ascend
ENV DEBIAN_FRONTEND=noninteractive
# Cap opc/c++/rust build parallelism to bound memory on shared buildkitd nodes:
# the heavy vllm-ascend compile otherwise spawns unbounded jobs and can exhaust
# a 128Gi node (OOM-killing kubelet). Overridable via --build-arg MAX_JOBS=N.
ARG MAX_JOBS=32
ARG CARGO_BUILD_JOBS=32
ENV SOC_VERSION=$SOC_VERSION \
    TASK_QUEUE_ENABLE=1 \
    OMP_NUM_THREADS=1 \
    MAX_JOBS=$MAX_JOBS \
    CARGO_BUILD_JOBS=$CARGO_BUILD_JOBS
COPY . /vllm-workspace/vllm-ascend/

RUN --mount=type=cache,target=/root/.cache/pip export PIP_EXTRA_INDEX_URL="${ASCEND_INDEX_URL}" && \
    export VLLM_BATCH_INVARIANT=1 && \
    git config --global url."http://git-cdn-service.git-cdn-gitcode.svc.cluster.local:8000/".insteadOf "https://gitcode.com/" && \
    source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    python3 -m pip install -e /vllm-workspace/vllm-ascend/ --extra-index-url ${PYTORCH_INDEX_URL} && \
    python3 -m pip uninstall -y triton triton-ascend && \
    python3 -m pip install triton-ascend==3.2.2 --extra-index-url ${ASCEND_INDEX_URL} && \
    python3 -m pip install concurrent-log-handler

# Install flash-linear-attention-npu from the Ascend mirror (aligned with vllm-ascend)
RUN source /usr/local/Ascend/ascend-toolkit/set_env.sh && \
    source /usr/local/Ascend/nnal/atb/set_env.sh && \
    export PIP_EXTRA_INDEX_URL="${ASCEND_INDEX_URL}" && \
    python3 -m pip install "flash-linear-attention-npu-a2===${FLA_VERSION}" --force-reinstall --no-deps

# Install _rust_tool_parser for the Rust frontend.
# The default toolchain is read from vllm's rust-toolchain.toml (falling back
# to stable).
ENV PATH="/root/.cargo/bin:$PATH"
RUN --mount=type=cache,target=/root/.cargo cd /vllm-workspace/vllm && \
    export PROTOC_INCLUDE=/usr/include && \
    python3 -m pip install setuptools-rust && \
    ./build_rust.sh

# Append `libascend_hal.so` path (devlib) to LD_LIBRARY_PATH
RUN echo "export LD_LIBRARY_PATH=$LD_LIBRARY_PATH:/usr/local/lib" >> ~/.bashrc

# ===== Conditional installation based on BUILD_TYPE =====

# Install daily packages via shared script
COPY .github/workflows/scripts/install_daily_deps.sh /tmp/
RUN if [ "$BUILD_TYPE" = "daily" ]; then \
        bash /tmp/install_daily_deps.sh; \
    else \
        echo "Building release version without daily packages"; \
    fi && rm -f /tmp/install_daily_deps.sh

CMD ["/bin/bash"]
