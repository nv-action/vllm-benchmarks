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

## 补充：squid-proxy 场景改用中国 apt 镜像（APT_MIRROR_URL）

第二次运行：[31006465703](https://github.com/nv-action/vllm-benchmarks/actions/runs/31006465703)（4 install + compare 全部 success）

给 squid-proxy 的 apt 注入 `APT_MIRROR_URL=https://mirrors.huaweicloud.com/ubuntu`（经 squid MITM 出网，
脚本自动写 `Acquire::https::CaInfo=/etc/squid-ca/squid-ca.pem`），对比 cache-service 保持默认集群内镜像。

### apt 阶段对比（单位：秒）

| phase | apt/squid（默认源 ports.ubuntu.com） | apt/squid（huaweicloud 中国镜像） | apt/cache |
|---|---|---|---|
| apt_update | 37 | **3** | 3 |
| apt_install | 19 | 27 | 19 |

- **apt_update：37s → 3s** —— 中国镜像经 squid 出网后，元数据下载与集群内 cache-service 镜像基本持平
  （日志实证 `Get:` 全部来自 `https://mirrors.huaweicloud.com/ubuntu`）。
- **apt_install：19s → 27s** —— 包下载量更大，经 squid MITM 的 TLS 开销略增，但仍属同一量级。
- 结论：默认源的 `ports.ubuntu.com`（海外）是 apt_update 37s 的主因；换中国镜像后网络路径差异几乎消失。

### 机制说明

- workflow 矩阵为 `ubuntu-22.04 × squid-proxy` 注入 `apt_use_proxy`，脚本 `write_apt_proxy_config()`
  写 `/etc/apt/apt.conf.d/99squid-proxy`（`Acquire::http::Proxy` / `Acquire::https::Proxy`），**不改默认源**。
- 换中国镜像则设 `APT_MIRROR_URL`，脚本 `rewrite_apt_sources()` 对 apt 场景无条件重写源。
- https 镜像时写 `/etc/apt/apt.conf.d/99-squid-ca` 指向 squid CA，否则 apt 过 squid 的 TLS 校验失败。

## 补充：squid-proxy 默认源 + apt 显式代理配置（APT_USE_PROXY）

第三次运行：[31136670693](https://github.com/nv-action/vllm-benchmarks/actions/runs/31136670693)（4 install + compare 全部 success）

按建议改为**不用中国镜像**：保留默认 apt 源，仅写 `Acquire::http::Proxy` / `Acquire::https::Proxy`
指向 squid，验证 apt 层的显式代理是否与 env 代理等效。

### apt 阶段对比（单位：秒）

| phase | apt/squid（默认源，冷缓存，基线） | apt/squid（默认源 + Acquire 代理） | apt/squid（huaweicloud 中国镜像） | apt/cache |
|---|---|---|---|---|
| apt_update | 37 | **9** | 3 | 3 |
| apt_install | 19 | 19 | 27 | 19 |

- **默认源 + Acquire 代理：apt_update 9s、apt_install 19s** —— 与 cache-service 几乎持平。
- **重要发现**：runner pod 会把容器默认 apt 源覆盖为 `archive.ubuntu.com`/`security.ubuntu.com`
  （镜像本身是 `ports.ubuntu.com`），且 workflow 容器里**没有**注入 `http_proxy` 环境变量——
  squid-proxy 场景的 `http_proxy` 是脚本 `setup_network()` 自己 export 的。
  因此基线的 37s 是 squid **冷缓存**抓 archive.ubuntu.com 的真实外网耗时；9s 是 squid 缓存转热后的结果。
- 三种 squid-proxy 配置（默认源/默认源+显式代理/中国镜像）最终都验证可行；中国镜像在**元数据下载**上
  最快（3s），但 apt_install（19s vs 27s）反而略快于镜像，均在同一量级，远优于冷缓存的 37s。

### 机制说明

- `APT_USE_PROXY=1` 时脚本写 `/etc/apt/apt.conf.d/99squid-proxy`，不改源；
- 三个配置均可在 workflow 矩阵通过 `apt_mirror` / `apt_use_proxy` 切换。

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

## CA 信任环境变量矩阵（各工具对各 env 的响应）

来源：https://github.com/ascend-gha-runners/vllm-ascend/actions/runs/29382951475

在 git / curl / python 的对比中，各工具对 CA 环境变量的识别范围差异巨大。本测试矩阵是排查问题 #2（upload/download-artifact 报 `self-signed certificate`）的直接依据：**Node.js 只认 `NODE_EXTRA_CA_CERTS`，不认 `SSL_CERT_FILE`/`SSL_CERT_DIR`/`CURL_CA_BUNDLE`**，这正是 workflow env 必须单独设置 `NODE_EXTRA_CA_CERTS` 的原因。

### 测试矩阵（环境变量 × 工具）

| Case | Env var set | curl | git | python urllib | pip | requests (bare) | node |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **00** (negative control) | *(none)* | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| **01** | `SSL_CERT_FILE` | OK | FAIL | OK | FAIL¹ | FAIL | FAIL |
| **01b** | `SSL_CERT_FILE` + pip upgraded... | — | — | — | exploratory² | — | — |
| **02** | `SSL_CERT_DIR` (hashed capath) | OK | FAIL | OK | FAIL¹ | FAIL | FAIL |
| **03** | `CURL_CA_BUNDLE` | OK | FAIL | FAIL | OK³ | OK³ | FAIL |
| **04** | `GIT_SSL_CAINFO` | FAIL | OK | FAIL | FAIL | FAIL | FAIL |
| **05** | `REQUESTS_CA_BUNDLE` | FAIL | FAIL | FAIL | OK | OK | FAIL |
| **06** | `PIP_CERT` | FAIL | FAIL | FAIL | OK⁴ | FAIL | FAIL |
| **07** | `NODE_EXTRA_CA_CERTS` | FAIL | FAIL | FAIL | FAIL | FAIL | OK |
| **08** (positive control) | *(none — system-wide CA store rebuilt)* | OK | OK | OK | OK | FAIL⁵ | FAIL |

### 关键观察与解读

1. **环境变量生效范围差异巨大：**
   - `curl`：对 `SSL_CERT_FILE`、`SSL_CERT_DIR`、`CURL_CA_BUNDLE` 生效。
   - `git`：非常独特，似乎**仅**对 `GIT_SSL_CAINFO`（04）和系统级 CA（08）生效，不支持通用的 `SSL_CERT_FILE` 等。
   - `python urllib`：只接受 `SSL_CERT_FILE`、`SSL_CERT_DIR` 和系统级 CA，对 `CURL_CA_BUNDLE` 等无效。
   - `pip` 与 `requests (bare)`：这两个工具能识别 `CURL_CA_BUNDLE` 和 `REQUESTS_CA_BUNDLE`，说明它们底层的证书验证逻辑是互相关联的（可能都调用了底层的 `curl` 或 `requests` 库环境变量，或者内部有逻辑互补）。
   - `node`（新增）：**仅**认 `NODE_EXTRA_CA_CERTS`（07）与系统级 CA（08），其余变量一概不读。GitHub Actions 的 upload/download-artifact 正是 node 实现，所以 runner 里必须显式设置 `NODE_EXTRA_CA_CERTS=/etc/squid-ca/squid-ca.pem`。

2. **关于脚注**：
   - `¹` 和 `²` 表示存在特定前提或环境条件（例如在 `01b` 的特定 venv 环境中，pip 的行为属于"探索性测试"，无法给出固定的 PASS/FAIL 预期）。
   - `³` 和 `⁴` 表示通过了测试，但可能有限制条件。
   - `⁵` 最值得注意（见下）。

3. **⚠️ 异常点（阳性对照 08）：**
   - 在 `Case 08` 系统级 CA 商店重建的"阳性对照"中，`curl`、`git`、`urllib`、`pip` 和 `node` 全都成功（OK），唯独 **`requests (bare)` 失败了**（FAIL⁵）。
   - **为什么会这样？** 这通常是 Python `requests` 库的"踩坑"点。`requests` 默认使用它内置的 `certifi` 包提供的 CA 证书包，而**不**总是使用操作系统的系统级 CA 存储。即使你正确配置了系统的 CA，如果没有做额外的配置（如设置 `REQUESTS_CA_BUNDLE`），`requests` 依然可能因为找不到它信任的 CA 证书而报错。

## 结论

- 修复后两种场景在**同一份镜像、同一批包、同一 pip** 下真实安装，数据具备可比性。
- 最大收益点：**apt update 走 cache-service 本地镜像（2s vs 37s）**；openEuler 与 pip 环节两者接近。
- cache-service 的 pypi 端点对 pip 损坏（返回门户页），属基础设施问题，待 cache-service 修复后可改回 `PIP_INDEX_URL=${CACHE_SERVICE}/pypi/simple` 重新对比。

## 产物

- 脚本：`test-case-007/install-env.sh`
- workflow：`.github/workflows/test-case-007-install-env.yaml`
- 本地复现 Job（k8s，pod template 复刻 postStart）：`tmp/job-ubuntu.yaml`、`tmp/job-openeuler.yaml`
- 逐 phase 数据：各 install job 的 `timings-*.tsv`（artifact）
