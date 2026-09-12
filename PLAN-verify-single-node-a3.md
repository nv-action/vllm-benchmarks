# 验证计划：single_node 单测同时验证「a3 runner」+「huaweicloud 镜像源」

> 分支：`feat/buildkitd-squid-test`（worktree: `.worktrees/wt-buildkitd-squid`）
> 状态：**单测流水线 dry-run 已通过（mock 模式）**；真实 NPU 用例待模型缓存预热后复跑

---

## 1. 背景与目标

### 目标
1. **验证 runner**：单测 job 能被用户指定的 `linux-aarch64-a3-2` 池子接住（而非 main config 里的 `linux-aarch64-nightly-a3-2`）
2. **验证镜像源**：`_e2e_nightly_single_node.yaml` 中 4 处 cache-service 已替换为华为云源，需实测生效（无 403/SSL/超时）

### 已确认事实
- `main` 分支 `weekly_config.yaml` / `nightly_config.yaml` 单测 `os` 全部是 `linux-aarch64-nightly-a3-*`（未授权的池）
- `feat/buildkitd-squid-test` 分支两个 config 单测 `os` 全部是 `linux-aarch64-a3-2/4/16`（用户指定池）
- weekly 与 nightly 都引用 `_e2e_nightly_single_node.yaml`，镜像源替换对两者都生效
- `vllm_ascend_ref` 在单测 workflow 内只在 `request_id != ''`（PR 模式）时才会在 pod 内 checkout；dispatch 模式（request_id 空）完全跳过，无副作用
- 镜像 tag 由 `vllm_ascend_branch` 决定；`nightly-ci-main-a3` 已存在，`nightly-ci-feat/buildkitd-squid-test-a3` 不存在

## 2. 方案

**用 nightly workflow，参数组合：**

| 参数 | 值 | 作用 |
|---|---|---|
| `vllm_ascend_branch` | `main` | 镜像 tag = `nightly-ci-main-a3`（存在） |
| `vllm_ascend_ref` | `feat/buildkitd-squid-test` | setup-vars 走 `./pr` sparse-checkout，矩阵读 **feature 分支** config（`linux-aarch64-a3-2`） |
| `test_cases` | `Qwen3.8-27B-w8a8-A3` | 只跑单测里这一个 a3-2 小模型 |
| `skip_build_image` | `true` | 复用已有镜像，不触发构建 |

**为什么不走 weekly**：weekly workflow 里还有临时 `diag-obs` job（L181），会跟着跑，噪音大。

### 预期效果
- 50+ 个 multi-node / double-node / 其余 single-node job 全部 `skipped`
- 仅 `single-node (main, Qwen3.8-27B-w8a8-A3, linux-aarch64-a3-2, Qwen3.8-27B-w8a8-A3.yaml)` 真正运行
- job 被 `linux-aarch64-a3-2` runner 接住 → runner 验证通过
- 容器内 UV/pip/apt 走 `repo.huaweicloud.com` / `mirrors.huaweicloud.com` 下载成功 → 镜像源验证通过

### 执行命令
```bash
gh workflow run schedule_nightly_test_a3.yaml \
  --repo nv-action/vllm-benchmarks \
  --ref feat/buildkitd-squid-test \
  -f vllm_ascend_branch=main \
  -f vllm_ascend_ref=feat/buildkitd-squid-test \
  -f test_cases=Qwen3.8-27B-w8a8-A3 \
  -f skip_build_image=true
```

## 3. 验证步骤

1. **dispatch 后**：确认新 run 的 job 列表——仅 1 个 single-node job 非 skipped，其余全 skipped
2. **job 开始后**：确认 `runs-on` runner 名为 `linux-aarch64-a3-2-*`（被 a3-2 池接住）
3. **日志检查**：
   - `Install clang` 步骤：apt 走 `mirrors.huaweicloud.com`，无 403
   - `Install vllm-ascend` / 相关 pip 步骤：走 `repo.huaweicloud.com`，无 SSL/403/超时
   - `Run Pytest`：模型测试正常执行（顺带确认 `/dev/shm` 是否还需要修）
4. **结束**：汇总结论——runner 可用性 + 镜像源是否生效

## 4. 后续清理（验证通过后）

- [ ] 移除 weekly workflow 里的临时 `diag-obs` job（L181 起），恢复 test_cases 行为
- [ ] OBS 403 根因已实锤（AccessDenied）——待用户更新 Secrets 或桶授权后，可用 `diag-obs` 方式复验
- [ ] 清理卡死的 weekly run 遗留（runner pod 是否还需集群侧强杀，看是否已自然结束）
- [ ] 本计划文档在验证完成后删除或归档

## 5. 风险与备注

- **runner 池可用性未知**：如果 `linux-aarch64-a3-2` 当前全部被卡死 run 占用，job 会继续排队——先确认卡死 run 的 runner 是否已释放
- 之前误发的 `34670362599`（等了错误的 nightly-a3-2 runner）已提交取消
- 若 huaweicloud 源下载失败，可对比 squid 日志确认是源问题还是网络问题

---

## 6. Mock 模式（跳过 NPU 用例，验证流水线其它环节）

### 目标
在**不占用 NPU、不下载模型、不编译 vllm** 的前提下，把 single_node 流水线的 其余全部环节（setup/镜像源/checkout/安装/上传）跑通并置绿。

### 实现（分支 `feat/nightly-a3-verify`）
- `tests/e2e/nightly/single_node/models/scripts/test_single_node.py`
  - 新增 `VLLM_ASCEND_MOCK_NPU=1` mock 分支：`_run_mock_npu_pipeline()`
  - 跳过：kv_pool_manager / vLLM serve / aisbench 基准 / 模型与数据集下载
  - 保留：结果 JSON 生成（perf + acc 假数据）、good_table 更新、结果上传
- `_e2e_nightly_single_node.yaml`
  - 新增 `mock_npu` input；pytest 步骤注入 `VLLM_ASCEND_MOCK_NPU`
  - **不重新编译**：mock 模式下新增「sparse checkout + 覆盖」步骤，把分支的纯 Python 测试/工具代码覆盖到镜像自带 `/vllm-workspace/vllm-ascend`，跳过 PR 模式的 uninstall/checkout/`pip install -e .`/aisbench 安装（guard 掉缺失的 sparse 目录）
- `schedule_nightly_test_a3.yaml`：新增 `mock_npu` dispatch input

### 结论：镜像代码 ≠ 分支代码（重要）
- 非 PR dispatch（`request_id` 为空）时，job 直接用镜像内置 `/vllm-workspace/vllm-ascend`，**不会**用分支代码
- 所以只改 workflow 参数不够，必须靠上面的 overlay 步骤把 mock 脚本带进 pod

## 7. 验证结果（2026-09-12）

### 已完成的 run 记录

| Run | 用途 | 结论 |
|---|---|---|
| `34685984719` | 首次 mock dispatch（无 overlay） | 容器跑的是镜像旧代码 → 真实 NPU 用例被误触发，cancel |
| `34686826739` | 改用 `request_id` 触发 PR 安装路径 | 触发 `pip install -e .` 编译 vllm-ascend（耗时且非必要），cancel |
| `34687373079` | overlay 方案 v1（只覆盖 1 个文件） | pytest 收集失败：镜像 tests 树缺少 `tests.e2e.nightly.scripts.profiling` |
| `34687811227` | overlay 方案 v2（覆盖 tests/tools/benchmark） | **pytest mock 通过（1 passed, 3.42s）**，good_table 更新、OBS/artifact 上传成功；但 overlay 步骤因 sparse 未检出 `benchmark/` 而 fail |
| `34688322364` | overlay 方案 v3（guard 缺失目录） | ✅ **job + run 全绿**（约 9 分钟） |

### 最终通过项（Run `34688322364`）
- [x] 镜像源：apt→`mirrors.huaweicloud.com`（`Install clang` 步骤实测生效）、pip/uv→`repo.huaweicloud.com`（无 403/SSL/超时）
- [x] runner：job 由 `linux-aarch64-a3-2` 池接住（`linux-aarch64-a3-2-*`）
- [x] 不编译：Install vllm-ascend / Install aisbench 在 mock 模式被跳过；overlay 步骤成功
- [x] `Run Pytest (YAML-driven)`：mock 通过（`1 passed in 3.42s`，无 NPU、无下载）
- [x] `Update good_table.csv on success`：成功
- [x] `Upload benchmark results`（OBS）：成功
- [x] `Upload benchmark results (GitHub Artifacts)`：成功（2195 B）

### 结果产物位置
- **OBS**（私有，需密钥）：
  `s3://ascend-ci-cache-hk@obs.ap-southeast-1.myhuaweicloud.com/artifacts/nv-action/vllm-benchmarks/34688322364/nightly-test-benchmark-results-main-Qwen3.8-27B-w8a8-A3-20260912T103046Z/`
- **GitHub Artifacts**（免鉴权）：run `34688322364` 的 Artifacts 页，同名 zip

### 遗留 / 下一步
- [ ] 真实 NPU 用例：27B 模型在 runner 缓存中不存在且下载超时；恢复 `HF_HUB_OFFLINE=1` 后需先用 `labeled_download_model_dataset`（或等价）预热模型/数据集，再跑非 mock 单测
- [ ] 真实用例此前 perf 校验失败（74 vs 95 token/s 基线）——与本次 dry-run 无关，需另行排查
- [ ] 验证完成后：清理 `feat/nightly-a3-verify` 临时分支、weekly `diag-obs` job、本计划文档

---

## 8. 镜像源替换范围（cache-service → huaweicloud）

### 本次已替换并实测通过（`_e2e_nightly_single_node.yaml`，dry-run 全绿）

| 环节 | 改前（cache-service） | 改后（华为云） |
|---|---|---|
| uv 主源 | `http://cache-service.nginx-pypi-cache.svc.cluster.local/pypi/simple` | `https://repo.huaweicloud.com/repository/pypi/simple` |
| uv 额外源 | —（新增） | `https://repo.huaweicloud.com/ascend/repos/pypi` + `UV_INDEX_STRATEGY=unsafe-best-match` |
| apt | `sed …@cache-service…:8081@g` 只改 `(ports|archive).ubuntu.com`、只改 `sources.list` | `sed …@mirrors.huaweicloud.com@g` 覆盖 `(ports|archive|security).ubuntu.com` + `sources.list` 与 `ubuntu.sources`（deb822）双格式，并 `grep -q` 校验必中 |
| pip index | `pip config set global.index-url http://cache-service…` | `https://repo.huaweicloud.com/repository/pypi/simple` |
| pip trusted-host | 单域 cache-service | 分两条 `pip config set global.trusted-host`：`repo.huaweicloud.com` + `files.pythonhosted.org`（一次传两域会语法报错） |
| pip 额外源 | — | `PIP_EXTRA_INDEX_URL`：华为 ascend 源 + triton-ascend + pytorch-cpu |

### 本次未改、仍指向 cache-service 的 workflow（如需统一替换再提）

- `_schedule_image_build.yaml`（构建镜像：APT/YUM/RUSTUP/PIP/PYTORCH/ASCEND 全走 cache-service，另有一套 buildkit 内部镜像源逻辑，属另一话题）
- `_e2e_nightly_single_node_560t.yaml`、`_e2e_nightly_single_node_models.yaml`（同款 3 处替换模式，未动）
- `schedule_main2main.yaml`、`_build_csrc_cache.yaml`、`schedule_e2e_upstream_test.yaml`、`_selected_tests.yaml`、`_selected_tests_upstream.yaml`、`pr_test.yaml`
- 说明：这些文件 90% 的 `repo.huaweicloud.com` 已是华为云源，仅 UV 主源/apt/pip 三处还挂在 cache-service 上；`Dockerfile.*` 已在用华为云源（`repo.huaweicloud.com` + `download.pytorch.org`）

### 为什么 openEuler 的 yum 替换不在本次范围
- 本次单测只跑 Ubuntu a3 容器；openEuler 的 `yum.repos.d/*.repo → cache-service:8081` 替换逻辑在 `_build_csrc_cache.yaml`（构建路径），未涉及。

### A/B 实测对比（mock 单测，2026-09-12）
- 方法：从 `feat/nightly-a3-verify` 派生临时分支 `feat/nightly-a3-verify-cs`，仅把 4 处镜像源改回 cache-service，其余（mock overlay、no_proxy、离线等）全部不变，跑同一个 `Qwen3.8-27B-w8a8-A3` mock job
- 两个 run 均全绿

| 步骤 | huaweicloud `34688322364` | cache-service `34689575957` |
|---|---|---|
| Check npu + pip config + pip install uv | 5s | 7s |
| Install clang（apt） | 9s | 8s |
| Run Pytest（YAML-driven, mock） | 46s | 51s |
| Upload benchmark results（OBS） | 12s | 11s |
| Upload（GitHub Artifacts） | 11s | 10s |
| **Job 总耗时** | **149s** | **150s** |

**结论**：mock 模式下镜像源差异属于噪声级别（每步 ±几秒，job 总量差 1s），替换不引入回退。原因：mock 不触发重下载（apt 只装 clang-15、pip 只装 uv，均为小包），cache-service（集群内缓存代理）与 huaweicloud（CDN）都足够快；真正吃镜像源带宽的 `pip install -r requirements-dev.txt` / vllm 编译 / 模型权重下载未被覆盖。
**如需量化吞吐差异**：改用 PR 模式（`request_id` 非空）跑 `Install vllm-project/vllm-ascend` 步骤，或直接对比大文件（如 27B 模型权重）下载耗时。
