# test-squid：CI 下载/上传全链路测试计划

## 1. 目标

在独立的 `test-squid` worktree（分支 `feat/test-squid`）中，把 vllm-ascend CI 里所有
「下载 / 上传」代码尽可能收敛到 **一个 workflow** 里跑一遍，验证 Squid 代理（已由 runner /
工作负载 pod 注入，workflow 内**不再配置任何代理**）对每一条链路的影响，并输出各阶段耗时。

- runner：`linux-amd64-cpu-4`
- 触发方式：`workflow_dispatch`
- 输出：各阶段耗时 TSV + 汇总表格 + 上传 artifact

## 2. CI 下载/上传全量清单（按类型分类）

### A. 系统包管理器下载（apt / yum / dnf）

| 子项 | 源码出处 | 真实载荷 |
| --- | --- | --- |
| `apt-get update` + `apt-get install` | `_schedule_image_build.yaml` 的 "Install csrc cache dependencies"；`_build_csrc_cache.yaml` 的 "Config mirrors (ubuntu)"；Dockerfile.a3 / openEuler | 数百 MB（packages.txt 全集） |
| `yum install` / `dnf install` | `_build_csrc_cache.yaml` 的 "Config mirrors (openeuler)"；Dockerfile.a3.openEuler | 数百 MB |
| apt 源替换（ports/archive.ubuntu.com → repo.huaweicloud.com） | `_schedule_image_build.yaml` L294-310 | - |

### B. Python 包下载（pip / uv）

| 子项 | 源码出处 | 真实载荷 |
| --- | --- | --- |
| `pip install`（huaweicloud pypi 源） | `_schedule_image_build.yaml` build-args `PIP_INDEX_URL`；`_build_csrc_cache.yaml` pip config | GB 级（requirements-dev.txt） |
| `uv pip install`（含 extra index） | `_build_csrc_cache.yaml`（`UV_INDEX_URL` / `UV_EXTRA_INDEX_URL` / `UV_INDEX_STRATEGY`） | GB 级 |
| pytorch CPU wheel（download.pytorch.org） | `_build_csrc_cache.yaml` L177-179；`install_daily_deps.sh` | ~100MB+ |
| ascend 私源（repo.huaweicloud.com/ascend/repos/pypi） | `_schedule_image_build.yaml` build-args `ASCEND_INDEX_URL` | 视包而定 |

### C. Git / GitHub 内容下载

| 子项 | 源码出处 | 真实载荷 |
| --- | --- | --- |
| `actions/checkout@v7`（仓库 + **Action 本体下载**） | 几乎所有 workflow | 几十 MB |
| `git fetch` / `git rebase` / submodule | `_build_csrc_cache.yaml` "Rebase on ... snapshot"、"Update submodules" | 中等 |
| `git clone` github.com 仓库 | Dockerfile.nightly.a3（AISBench benchmark，经 gh-proxy） | 中大型 |
| git 直连 github.com / 经 gh-proxy | `main2main-e2e.yaml`、`_schedule_image_build.yaml` `GIT_PROXY` | 视仓库而定 |

### D. 模型 / 数据集下载

| 子项 | 源码出处 | 真实载荷 |
| --- | --- | --- |
| `modelscope download`（模型 + 数据集） | `labeled_download_model_dataset.yaml` | GB 级 |
| HuggingFace 下载（`HF_HUB_OFFLINE` 开关） | `tools/aisbench.py` | GB 级 |

### E. 镜像仓库拉取

| 子项 | 源码出处 | 真实载荷 |
| --- | --- | --- |
| job `container.image` 拉取 | 所有 NPU/构建 workflow | 数百 MB~GB |
| `docker build` registry cache（cache-from） | `_schedule_image_build.yaml` L369 | 依赖构建 |
| skopeo / docker pull SWR 镜像 | `_schedule_image_build.yaml` | 数百 MB |

> 注：SWR/registry 域在 Squid 配置中不 MITM（`ssl_bump` 排除 registry），拉取走直连，仅记录耗时。

### F. 制品 / 缓存上传下载

| 子项 | 源码出处 |
| --- | --- |
| `actions/upload-artifact@v7` | `_schedule_image_build.yaml` L412-418 |
| `actions/download-artifact@v8` | `_schedule_image_build.yaml` L439-451 |
| `runs-on/cache/save@v5`（→ OBS） | `push_build_precommit_cache.yaml`、`_build_csrc_cache.yaml` |
| `runs-on/cache/restore@v5` | `_ensure_csrc_cache.yaml`、`_schedule_image_build.yaml` |

### G. OBS 对象存储上传 / 下载

| 子项 | 源码出处 |
| --- | --- |
| `esdk-obs-python` `ObsClient.putFile` | `schedule_release_code_and_wheel.yml`（注释中的 generate_and_upload_variant_index） |
| OBS wheel 下载（wget） | `install_daily_deps.sh`（torch-npu / memfabric / memcache / triton） |

### H. 镜像复制 / 推送

| 子项 | 源码出处 |
| --- | --- |
| `skopeo copy` 打临时 tag | `_schedule_image_build.yaml` L374-403 |
| `docker buildx imagetools create` | `_schedule_image_build.yaml` L541-571 |

### I. 直接网络请求（curl / wget 探测）

| 子项 | 源码出处 |
| --- | --- |
| wget OBS 大文件 | `install_daily_deps.sh` |
| curl 链接检查 | `schedule_doc_linkcheck.yaml`（check_md_links.py） |

### J. Git push（上传到 GitHub）

| 子项 | 源码出处 |
| --- | --- |
| `git push` 到 fork（经 gh-proxy / git-cdn / 直连多路由） | `main2main-push-probe.yaml`（`_push_via_proxy`） |

## 3. Mock 策略（下载太慢 / 载荷太大时的替代）

原则：**保留真实的 URL 路径与工具链**，只把「载荷」缩小，让每条链路的网络行为不变但耗时可控。

| 原链路 | 真实载荷 | Mock 方式 |
| --- | --- | --- |
| `apt-get install < packages.txt 全集>` | 数百 MB | 只装 `zstd`（小包，同 `_schedule_image_build.yaml` csrc 依赖） |
| `pip install -r requirements-dev.txt` | GB 级 | `pip install` 2~3 个小轮子（如 `zstandard`、`requests`），仍走真实 index |
| `uv pip install` 全量 | GB 级 | `uv pip download` 一个纯 py 小包，验证 extra index 解析 |
| pytorch CPU wheel | ~100MB | `pip download --no-deps` 只取 `.metadata`（PEP 658），不拉 wheel 本体 |
| OBS wheel（memfabric/triton/torch-npu） | 100MB+ | 对同一 bucket 发 `wget` 一个小文件，验证 OBS 下载路径 |
| `git clone` AISBench 大仓库 | 大仓库 | `git ls-remote` + `git clone --depth 1 --filter=blob:none` 小仓库 |
| `modelscope download` 模型 | GB 级 | 仅安装 modelscope（走 pip 路径），模型下载标记为「跳过/可选」，可配 `TEST_SQUID_DOWNLOAD_MODEL=1` 打开真下 |
| artifact / cache | 真实产物 | 生成 1 个小文件往返 + sha256 校验 |
| OBS 上传（esdk） | wheel 集合 | 上传 1 个小文本对象到测试前缀；无写权限则跳过（continue-on-error） |

## 4. Workflow 设计

文件：`.github/workflows/test-squid-download-upload.yaml`

```
on: workflow_dispatch

jobs:
  download-upload-suite:          # matrix: ubuntu / openeuler，均跑在 linux-amd64-cpu-4
    runs-on: linux-amd64-cpu-4
    container: cann:9.1.0-a3-<ubuntu22.04|openeuler24.03>-py3.12   # 与 _build_csrc_cache 相同
    steps:
      1. 环境探测：打印 HTTP(S)_PROXY / SSL 相关 env，确认 squid 注入生效（只读，不配置）
      2. A 系统包：apt-get update + install zstd / yum install zstd（真实命令）
      3. B pip：pip install 小轮子 + uv pip download 小包 + pytorch 元数据下载
      4. C git：git ls-remote github.com + 浅克隆小仓库
      5. D 模型：安装 modelscope（可选下载极小型模型）
      6. I 直连探测：curl 各域名连通性 + 延迟（github / raw.githubusercontent / pypi 镜像 / pytorch / modelscope / OBS）
      7. 上传类：本地造 1 个小文件
         - F actions/upload-artifact（上传）
         - F runs-on/cache/save + restore（小 key，走 OBS 后端）
         - G esdk-obs-python 上传小对象（无凭据则跳过）
         - H skopeo copy SWR 小镜像 tag（无凭据则跳过）
         - J git push 探测分支（无 PAT 则跳过）
      8. 每步计时写入 timings TSV + 汇总
      9. 上传 timings 制品

  summary:
    needs: download-upload-suite
    steps: actions/download-artifact 拉全部 timings → 汇总表格输出
```

不做的：
- 不在 workflow 中注入/配置代理（squid 已由 runner pod 注入）
- 不做镜像 build / NPU 测试（本测试只关心网络下载/上传链路）

## 5. 文件结构

```
test-squid/
  PLAN.md               # 本计划
  run-suite.sh          # 下载/上传测试主脚本（命令提取自上述 CI 源码）
.github/workflows/
  test-squid-download-upload.yaml   # 唯一测试 workflow
```

## 6. 验收标准

1. workflow 在 `linux-amd64-cpu-4` 上可手动触发跑通（apt/pip/git/curl 等 mock 后步骤全部成功或明确跳过）。
2. 输出 timings TSV：每个下载/上传阶段都有耗时与状态，可用于对比 squid 命中/未命中。
3. 上传链路（artifact / cache / OBS / skopeo / git push）在凭据可用时执行真实上传并校验。
4. 脚本可重复运行、幂等，失败不影响其它阶段（fail-fast: false，阶段级 continue-on-error）。

## 7. 待确认/风险

- `linux-amd64-cpu-4` 上是否已部署 squid 注入的 runner pod 模板（前例用的 `linux-amd64-cpu-4-buildkit-gy006`）。
- OBS 直传凭据的写权限 bucket（历史记录：`ascend-ci-cache-hk` 对 `OBS_ACCESS_KEY_ID/SECRET` 403）。
- runs-on/cache 后端使用 `HW_OBS_AK/HW_OBS_SK`，保存小 key 是否可写 `ascend-ci-cache-hk`。
