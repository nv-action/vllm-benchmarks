# test-case-007 测试报告：squid-proxy vs cache-service 安装耗时对比

日期：2026-08-05

[![workflow status](https://github.com/nv-action/vllm-benchmarks/actions/workflows/test-case-007-install-env.yaml/badge.svg)](https://github.com/nv-action/vllm-benchmarks/actions/workflows/test-case-007-install-env.yaml)

最终运行：[31003932022](https://github.com/nv-action/vllm-benchmarks/actions/runs/31003932022)（compare job：4 个 install job + compare 全部 success）

## 目的

对比 buildkit runner 上同一条 apt/yum + pip 安装脚本在两种网络环境下的耗时：

| 场景 | 网络路径 | 说明 |
|---|---|---|
| `squid-proxy` | 走 squid 代理（HTTP(S)_PROXY=`squid-cache.squid.svc.cluster.local:3128`，MITM SSL-bump） | runner 默认配置，pip 用 tuna 镜像 |
| `cache-service` | unset 代理，直连集群内 cache-service（apt/yum 走其镜像缓存；pip 因 cache-service pypi 损坏回退 tuna） | 直连路径 |

## 环境

- runner：`linux-amd64-cpu-4-buildkit-gy006`（amd64 buildkit，k8s pod 模板带 squid 代理 + `/etc/squid-ca` CA）
- 镜像：
  - ubuntu：`base_image/ascend-ci/cann:9.0.1-a3-ubuntu22.04-py3.12`（实际 codename=jammy）
  - openeuler：`base_image/ascend-ci/cann:9.0.1-a3-openeuler24.03-py3.12`（实际 openEuler 24.03 LTS-SP3）
- python：镜像自带 `/usr/local/python3.12.13/bin/python3` + pip 26.1.2（脚本经 `find_python()` 优先选用）
- 安装内容（提取自 Dockerfile.a3 / .openEuler）：
  - apt：`git vim wget net-tools gcc g++ cmake numactl libnuma-dev libibverbs-dev libjemalloc2 libhiredis-dev clang-15`
  - yum：`git vim wget net-tools gcc gcc-c++ make cmake numactl numactl-devel libibverbs-devel jemalloc hiredis-devel clang patch`
  - pip：`mooncake-transfer-engine-npu==0.3.11.post1`、`modelscope`、`ray>=2.47.1,<=2.48.0`、`protobuf>3.20.0`

## 结果（单位：秒）

| phase | apt/squid | apt/cache | yum/squid | yum/cache |
|---|---|---|---|---|
| apt_update / yum_update | 37 | **2** | 138 | 142 |
| apt_install / yum_install | 19 | 19 | 82 | 72 |
| pip_config | 0 | 0 | 0 | 0 |
| pip_mooncake | 8 | 23 | 8 | 7 |
| pip_misc | 26 | 21 | 22 | 19 |
| **合计** | **90** | **65** | **250** | **240** |

> pip_config=0s 为本地写配置，属正常（非失败）。全部 phase 状态为 OK。

### 主要观察

- **apt update：cache-service 2s vs squid 37s** —— 本地缓存镜像比 squid 代理快一个量级（最大差异项）。
- **apt install 相同（19s/19s）**：两个场景安装的是同一批包。
- **yum update/install 两场景接近（138/142、82/72）**：openEuler 元数据经 cache-service 无明显加速。
- **pip 阶段整体接近**：squid 与直连差异不大（apt 的 pip_mooncake squid 反而更快 8s vs 23s，可能与 tuna/huaweicloud 缓存命中有关）。

## 过程中发现并修复的问题

| # | 问题 | 根因 | 修复 |
|---|---|---|---|
| 1 | 容器内 job 未起即退出（exit 137） | 基础镜像 `modelfoundry/ubuntu:24.04`/`openeuler:24.03` 无 `git`，runner postStart 的 `git config --system` 无保护执行失败 → kubelet SIGKILL 容器（137） | 改用 CANN 镜像（自带 git 等工具链） |
| 2 | upload/download artifact 报 `self-signed certificate` | Node.js 不认 `SSL_CERT_FILE`/`CURL_CA_BUNDLE`，只认 `NODE_EXTRA_CA_CERTS`；runner pod 未设置 | workflow env 加 `NODE_EXTRA_CA_CERTS=/etc/squid-ca/squid-ca.pem` |
| 3 | compare 步骤 `sort: multi-character tab '$\\t'` | `$'\t'` 为 bash 语法，容器默认 `sh`(dash) 不识别 | compare 步骤 `shell: bash` |
| 4 | cache-service apt 全 0s | CANN ubuntu 镜像无 `/etc/apt/sources.list.d/`，脚本写源失败 → 零 apt 源；且该镜像 `python3` 不在 PATH | `rewrite_apt_sources` 加 `mkdir -p`；keyring 缺失时回退 `Trusted: yes` |
| 5 | cache-service yum 全失败 | 镜像为 openEuler 24.03 **LTS-SP3**，cache-service 目录结构是 `openEuler-24.03-LTS-SP3/OS|EPOL/...`，原脚本用 `$releasever/os` 404 | 复刻镜像默认仓库集，`https://repo.openeuler.org` → `${CACHE_SERVICE}/openeuler`，gpgcheck=0、skip_if_unavailable |
| 6 | cache-service pip 失败 | `${CACHE_SERVICE}/pypi/simple` 301→`mirrors.huaweicloud.com`，对 pip 的 PEP 691 Accept 头返回门户页（0 包链接）；且 runner PATH 排除镜像 python → 用了系统 pip 22.0.2 | cache-service pip 回退 tuna；`find_python()` 优先用镜像自带 pip 26 |
| 7 | 矩阵名称不符 | 写 `ubuntu-24.04` 实为 22.04 | 改 `ubuntu-22.04` |

## 结论

- 修复后两种场景在**同一份镜像、同一批包、同一 pip** 下真实安装，数据具备可比性。
- 最大收益点：**apt update 走 cache-service 本地镜像（2s vs 37s）**；openEuler 与 pip 环节两者接近。
- cache-service 的 pypi 端点对 pip 损坏（返回门户页），属基础设施问题，待 cache-service 修复后可改回 `PIP_INDEX_URL=${CACHE_SERVICE}/pypi/simple` 重新对比。

## 产物

- 脚本：`test-case-007/install-env.sh`
- workflow：`.github/workflows/test-case-007-install-env.yaml`
- 本地复现 Job（k8s，pod template 复刻 postStart）：`tmp/job-ubuntu.yaml`、`tmp/job-openeuler.yaml`
- 逐 phase 数据：各 install job 的 `timings-*.tsv`（artifact）
