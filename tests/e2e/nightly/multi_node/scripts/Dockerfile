FROM quay.nju.edu.cn/ascend/cann:8.3.rc1-ubuntu22.04-py3.11

# ---------------------------------------------------------------------
# ARG & ENV
# ---------------------------------------------------------------------
ARG PIP_INDEX_URL="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
ARG COMPILE_CUSTOM_KERNELS=1
ARG VLLM_WORKSPACE="/vllm-workspace"
ARG MOONCAKE_TAG="v0.3.7.post2"

ENV DEBIAN_FRONTEND=noninteractive \
    COMPILE_CUSTOM_KERNELS=${COMPILE_CUSTOM_KERNELS} \
    WORKDIR=workspace

WORKDIR $WORKDIR

SHELL ["/bin/bash", "-c"]
# ---------------------------------------------------------------------
# Setup pip
# ---------------------------------------------------------------------
RUN pip config set global.index-url ${PIP_INDEX_URL}

COPY ./Mooncake /vllm-workspace/Mooncake
COPY ./benchmark /vllm-workspace/benchmark
COPY ./vllm /vllm-workspace/vllm
COPY ./mooncake_installer.sh ./mooncake_installer.sh

# Install mooncake
RUN chmod +x ./mooncake_installer.sh && \
    ./mooncake_installer.sh ${MOONCAKE_TAG}

RUN apt-get update -y && \
    apt-get install -y --no-install-recommends \
    git vim wget net-tools gcc g++ cmake && \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*

# Install vLLM
RUN VLLM_TARGET_DEVICE="empty" pip3 install -v -e $VLLM_WORKSPACE/vllm/ \
        --extra-index-url https://download.pytorch.org/whl/cpu/ && \
    pip3 uninstall -y triton

# Install benchmark
RUN pip3 install --no-cache-dir -e $VLLM_WORKSPACE/benchmark -r $VLLM_WORKSPACE/benchmark/requirements/api.txt -r $VLLM_WORKSPACE/benchmark/requirements/extra.txt && \
    pip3 install pybind11 && \
    pip3 cache purge

CMD ["/bin/bash"]
