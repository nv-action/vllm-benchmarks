# 构建链路 CA 信任问题调查记录（pip / BuildKit MITM / Squid）

> 场景：镜像构建走 BuildKit `--proxy-network`，上游代理为 squid（HTTPS_PROXY=http://squid-cache.squid.svc.cluster.local:3128），squid 对除白名单外的域名做 `ssl_bump`（MITM）。
> 本文记录"为什么 pip 在容器里会报 `CERTIFICATE_VERIFY_FAILED`"的取证过程，以及各客户端的信任机制对照。

## 完整链路

```
pip (RUN 容器内, exec 进程)
  │  ① HTTPS —— BuildKit 内部 proxy 用自签 MITM CA 终止 TLS
  ▼
BuildKit 内部 proxy (--proxy-network)
  │  ② 明文 HTTP 转发（HTTPS_PROXY=http://squid-cache.squid.svc.cluster.local:3128）
  ▼
squid (ssl_bump MITM, 用自己的 SquidCacheCA 与上游做 TLS)
  ▼
上游 (repo.huaweicloud.com 等)
```

- 第 ② 跳（buildkit → squid）是**明文 HTTP**，无 TLS、无 CA 校验问题（上游代理 URL 是 `http://`）。
- 第 ① 跳（pip → buildkit proxy）是 HTTPS，由 **BuildKit 的 MITM CA** 签发证书。
- BuildKit 会把它的 MITM CA 注入容器**系统信任库**（proxy.md 原文："injects a generated CA certificate into common Linux trust bundle locations ... using the system trust store"）。
- **但 pip 不用系统信任库**——这是全部问题的根源。

## 证据 1：pip 显式加载 certifi，而不是系统库（最硬的证据）

`pip/_internal/cli/index_command.py` 中 pip 构建 SSL context 时硬编码加载 certifi：

```python
ctx = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.load_verify_locations(certifi.where())   # ← 显式加载 certifi，不读系统库
```

功能实证（用 pip 自身逻辑跑）：

```
pip 实际使用的 CA 文件 = .../pip/_vendor/certifi/cacert.pem
ssl 默认系统 CA 文件   = /etc/ssl/cert.pem
两者是否为同一文件     = False
```

## 证据 2：pip vendored requests 默认也指向 certifi

`pip/_vendor/requests/utils.py`：

```python
# Certificate is extracted by certifi when needed.
DEFAULT_CA_BUNDLE_PATH: str = certs.where()
```

## 证据 3：certifi 与系统信任库是两个不同文件、内容不同

| 文件 | CA 数 | 说明 |
|---|---|---|
| `pip/_vendor/certifi/cacert.pem` | 118 | pip 实际使用的 bundle |
| `/etc/ssl/certs/ca-certificates.crt` | 121 | 系统信任库（BuildKit 注入 CA 的目标） |

`cmp` 结果：**不同文件**。所以 BuildKit 把 MITM CA 追加进系统库后，只影响读系统库的工具；pip 的 certifi 文件里没有它 → 必然报 `unable to get local issuer certificate`。

## 结论 / 修复

"pip 信任 buildkit CA 就无验证问题"需要**把 pip 指到系统库**才成立：

```dockerfile
ENV PIP_CERT=/etc/ssl/certs/ca-certificates.crt   # Ubuntu（系统库已含 BuildKit 注入的 CA → 验证通过）
ENV PIP_CERT=/etc/pki/tls/certs/ca-bundle.crt     # openEuler
```

`PIP_CERT` 是 `--cert` 选项的 env 形式（`pip/_internal/cli/cmdoptions.py`: "Path to PEM-encoded CA certificate bundle"），会覆盖默认的 certifi。

> 注意：构建部署里 buildkitd 的 `SSL_CERT_FILE=/ca-merged/ca-bundle.pem` 对 pip **无效**——`SSL_CERT_FILE` 只被 buildkitd 守护进程和原生工具认，pip 的 `ctx.load_verify_locations(certifi.where())` 根本不看它。

## 各客户端 CA 信任机制对照（参考 workflow 实证）

参考验证 workflow：
https://github.com/ascend-gha-runners/vllm-ascend/actions/runs/29382951475/workflow

（`test-case-007-env-ca`：在 ubuntu-24.04 / openeuler-24.03 裸镜像上，对 curl / wget / git / python3 urllib / pip / 裸 requests / apt / yum 做信任矩阵验证，squid 金丝雀检测确保流量确实被 MITM。）

| 机制 | curl | wget | git | py urllib | pip | 裸 requests |
|---|---|---|---|---|---|---|
| （什么都不配） | FAIL | FAIL | FAIL | FAIL | FAIL | FAIL |
| `SSL_CERT_FILE` / `SSL_CERT_DIR`（OpenSSL 默认路径） | OK | OK | FAIL | OK | FAIL* | FAIL |
| `CURL_CA_BUNDLE`（requests 库当 fallback 读） | OK | FAIL | FAIL | FAIL | OK | OK |
| `GIT_SSL_CAINFO` | FAIL | FAIL | OK | FAIL | FAIL | FAIL |
| `REQUESTS_CA_BUNDLE`（requests 库读） | FAIL | FAIL | FAIL | FAIL | OK | OK |
| `PIP_CERT`（pip 自己读，显式传给内部 session） | FAIL | FAIL | FAIL | FAIL | OK | FAIL |
| `GNUTLS_CA_FILE` / `GNUTLS_CA_DIR`（仅 openEuler 的 GnuTLS wget） | FAIL | OK* | FAIL | FAIL | FAIL | FAIL |
| 系统信任重建（update-ca-certificates / update-ca-trust extract） | OK | OK | OK | OK | OK | **FAIL** |

要点：

- **pip 内部用 requests 库发请求**，所以 `CURL_CA_BUNDLE` / `REQUESTS_CA_BUNDLE` 对 pip 也生效（这是 requests 库 `Session.merge_environment_settings()` 的行为，不是 pip 自己的逻辑）。
- **裸 requests 只认内置 certifi**：连系统信任重建都救不了它，是这张表里唯一的盲区。
- *pip 的 `SSL_CERT_FILE` 行为版本相关：实测 stock pip 24.0（apt 默认）不认该变量（pip>=22.2 的 truststore 功能默认关闭），升级 pip 后行为需单独验证（见 workflow Case 01b）。
- *wget 的 GnuTLS 变量只在 openEuler（wget 用 GnuTLS 编译）生效。

## 补充参考

- BuildKit proxy 机制文档：https://github.com/tonistiigi/buildkit/blob/71bb8734c813346ea03abf6674ff1ca20fe16d0d/docs/proxy.md
- 部署事实：buildkitd 以 `--proxy-network` 启动；上游 `HTTPS_PROXY=http://squid-cache.squid.svc.cluster.local:3128`；`SSL_CERT_FILE=/ca-merged/ca-bundle.pem`（对 pip 无效）。
