# test-case-007: apt/yum + pip 安装耗时对比（squid 代理 vs cache-service）

## 背景

对比在 buildkit runner 上，用两种网络环境跑**同一份** apt/yum + pip 安装脚本的时间：

| 场景 | 网络配置 | 说明 |
|---|---|---|
| `squid-proxy` | `http_proxy=squid-cache.squid.svc.cluster.local:3128`（runner 默认） | 走 squid 代理，pip 用 tuna 镜像（Dockerfile 默认） |
| `cache-service` | unset `http_proxy`，使用 APTMIRROR / PIP_INDEX_URL / PYTORCH_INDEX_URL / ASCEND_INDEX_URL / GIT_PROXY | 直连集群内 cache-service 缓存 |

## 提取的安装步骤（来自 Dockerfile.a3 / Dockerfile.a3.openEuler）

只提取不依赖 CANN 工具链和 vllm 源码检出（git clone）的部分：

### Ubuntu (apt)

```bash
apt-get update -y
apt-get install -y git vim wget net-tools gcc g++ cmake numactl libnuma-dev \
    libibverbs-dev libjemalloc2 libhiredis-dev clang-15
python3 -m pip install mooncake-transfer-engine-npu==0.3.11.post1 \
    --extra-index-url https://mirrors.aliyun.com/pypi/web/simple
python3 -m pip install modelscope 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0'
```

### openEuler (yum)

```bash
yum update -y
yum install -y git vim wget net-tools gcc gcc-c++ make cmake numactl numactl-devel \
    libibverbs-devel jemalloc hiredis-devel clang patch
python3 -m pip install mooncake-transfer-engine-npu==0.3.11.post1 \
    --extra-index-url https://mirrors.aliyun.com/pypi/web/simple
python3 -m pip install modelscope 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0'
```

> 未包含：vllm `pip install -e /vllm-workspace/vllm`、vllm-ascend `pip install -e`、triton-ascend
> （它们依赖 CANN 环境 `/usr/local/Ascend/...` 和源码检出，不在本次网络耗时对比范围）。

## 运行

```bash
gh workflow run test-case-007-install-env.yaml --repo ascend-gha-runners/vllm-ascend --ref main
```

矩阵：`os(ubuntu-22.04, openeuler-24.03) × scenario(squid-proxy, cache-service)`，
runner `linux-amd64-cpu-4-buildkit-gy006`（amd64 buildkit runner，镜像为 CANN 基础镜像）。
每个组合生成 `timings-<os>-<scenario>.tsv`（phase / seconds / scenario / os），
`compare` job 汇总并输出两种场景的总耗时。

> 注意：CANN 基础镜像 `python3` 可能不在 PATH / 无 pip，脚本会往安装列表追加 `python3-pip`。
> Node.js action（upload/download artifact）不认 `SSL_CERT_FILE`，需设 `NODE_EXTRA_CA_CERTS=/etc/squid-ca/squid-ca.pem`。

## 假设 / 需按实际集群调整

- `cache-service` 场景下 apt 源改为 `${CACHE_SERVICE}/ubuntu`（Ubuntu Debian822，codename 取镜像实际值，
  如 jammy）。部分基础镜像无 `/etc/apt/sources.list.d/`，脚本会先 `mkdir -p`。
- yum 源默认改为复刻镜像自带仓库集（OS/everything/EPOL/update/...），
  把 `https://repo.openeuler.org` 前缀换成 `${CACHE_SERVICE}/openeuler`
  （cache-service 的 openEuler 目录结构是 `openEuler-<ver>-LTS-SPx/<repo>/<arch>/`）。
  若镜像目录结构不同，用 `YUM_MIRROR_URL` 覆盖镜像基址（替换 `https://repo.openeuler.org`）。
- cache-service 的 pip 索引 `${CACHE_SERVICE}/pypi/simple` 会 301 到 `https://mirrors.huaweicloud.com/pypi/simple/`。
  该场景会 unset pod 注入的 squid CA 变量（PIP_CERT/SSL_CERT_FILE 等），否则直连时 TLS 校验失败。
- squid 场景下需要信任 squid MITM CA（job pod template 会挂到 `/etc/squid-ca/squid-ca.pem`，
  脚本会追加到 certifi）。
- 如果测试基础镜像没有 pip，脚本会在 apt/yum 安装列表里追加 `python3-pip`（同样计入耗时）。

## 文件说明

- `install-env.sh` — 核心脚本：按场景配置网络环境，逐阶段计时（apt/yum update、install、pip install），输出 tsv + log
- `.github/workflows/test-case-007-install-env.yaml` — 矩阵 workflow + compare 汇总 job
