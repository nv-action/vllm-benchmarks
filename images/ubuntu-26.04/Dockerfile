# Ubuntu 26.04 LTS 基础镜像(软件镜像的底座)。
# 固化构建工具链,便于在其上叠装被测软件、离线化。
# 构建(aarch64 Runner 上):
#   docker build -t kunpeng/ubuntu-26.04.1:base images/ubuntu-26.04
# 软件镜像以本镜像为底座,按 <仓库>:<软件>-<版本> 命名,例如:
#   FROM kunpeng/ubuntu-26.04.1:base  =>  kunpeng/ubuntu-26.04.1:redis-8.10.2
# 建议记录镜像 digest,锁定 ubuntu:26.04 的实际内容。
FROM swr.cn-north-4.myhuaweicloud.com/ddn-k8s/docker.io/library/ubuntu:26.04

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        git \
        build-essential \
        tcl \
        python3 \
        procps \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /root