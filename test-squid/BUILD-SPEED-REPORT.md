# 移除内部镜像源后的构建速度报告（vs PR #306）

日期：2026-09-21
分支：`feat/remove-internal-mirrors`（PR #340）
对比基线：PR #306（`feat/switch-quay-to-swr-southwest-2`）——本分支的直接父分支，可隔离"镜像源移除"这一单一变量。

## 背景前提

- 本分支已移除全部内部 cache-service 镜像（APTMIRROR/YUM_MIRROR/pypi 缓存等），apt/yum/pip 全部经 squid 出公网。
- squid 对高热点包走 `refresh_pattern` 缓存；buildkitd 已部署 rootful 模式 + proxy CA 注入（anchors 方案）。
- 2026-09-20 晚清空过 squid HTTP 缓存与 buildkit 孤儿快照，热点缓存随 CI 运行逐步回温。

## Build and push 步骤耗时对比（三次全绿 run）

| target | PR #306<br>(35508831146, 09-20) | 本分支首绿<br>(35515117396, 09-20) | 本分支最新<br>(35570181610, 09-21) | 最新 vs #306 |
|---|---|---|---|---|
| openEuler amd64 | 14m37s | 20m22s | 15m07s | +30s |
| Ubuntu arm64 | 13m02s | 11m12s | 13m19s | +17s |
| openEuler arm64 | 12m45s | 15m33s | 14m57s | +2m12s |
| Ubuntu amd64 | 12m54s | 20m11s | 15m17s | +2m23s |
| **四路合计** | **53m18s** | **67m18s** | **58m40s** | **+5m22s (+10%)** |

## 结论

1. 首绿 run（缓存全冷、基础设施抖动多）实测 +26%；缓存回温后最新 run 收窄至 **+10%**。
2. 剩余差距构成：squid 相对内部 cache-service 的网络路径开销 + amd64 双 job 共享 4 核 runner 的争抢噪声。缓存越热越接近基线，预期稳定在 ~10% 量级。
3. 若需进一步收窄，可选优化（均不引入"内部镜像"语义）：
   - squid 对 `pypi.org/simple`、`files.pythonhosted.org`、`repo.openeuler.org` 增加更长的 refresh_pattern；
   - amd64 两路错峰或拆到独立 runner，消除争抢。

## 附：同期基础设施事故记录（chown-cache）

- 现象：A3 Ubuntu arm64 多次在 `pip cache purge` 失败（"cache is disabled" / not owned）。
- 根因：gy-006 buildkitd 提交 `9c5f1b89` 切 rootful 后，遗留 rootless 时代的 `chown -R 1000:1000` init 容器，把 PVC 上旧缓存层（含基础镜像 `/root`）属主改为 1000，而 daemon/构建容器 euid=0，pip 属主检查失败导致缓存禁用。
- 修复：移除 init 容器 + 存量文件一次性 `chown -R 0:0`；探针验证通过。openEuler job 当时为绿仅因其 base 镜像层在重启后新拉取、未被 chown 污染。
- 待办：chart 仓库中清除 `chown-cache` 模板，防止 ArgoCD 同步后复发。
