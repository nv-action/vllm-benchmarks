# openEuler 24.03 LTS SP4 基础镜像(软件镜像的底座)。
# 固化构建工具链,便于在其上叠装被测软件、离线化。
# 构建(aarch64 Runner 上):
#   docker build -t kunpeng/openeuler-24.03-sp4:base images/openeuler-24.03
# 软件镜像以本镜像为底座,按 <仓库>:<软件>-<版本> 命名,例如:
#   FROM kunpeng/openeuler-24.03-sp4:base  =>  kunpeng/openeuler-24.03-sp4:redis-8.10.2
# 建议记录镜像 digest,锁定 openeuler/openeuler:24.03-lts-sp4 的实际内容。
FROM openeuler/openeuler:24.03-lts-sp4

RUN dnf install -y --setopt=install_weak_deps=False \
        bash \
        ca-certificates \
        curl \
        git \
        make \
        gcc \
        gcc-c++ \
        tcl \
        python3 \
        procps-ng \
        tar \
        gzip \
    && dnf clean all

WORKDIR /root