# test-squid 测试报告

> Squid 代理注入 runner 上 vllm-ascend CI 下载/上传链路验证报告
> 方案：[PLAN.md](./PLAN.md) ｜ 执行分支：`feat/test-squid` ｜ PR：[#336](https://github.com/nv-action/vllm-benchmarks/pull/336)
> 报告日期：2026-09-17

---

## 1. 测试目的

把 vllm-ascend CI 中所有「下载 / 上传」代码收敛到一个 workflow（`test-squid-download-upload`），在 Squid 代理（`ssl_bump bump all` MITM，CA 由 runner/workload pod 注入）环境下逐链路验证连通性并输出各阶段耗时，为后续把 CI runner 迁移到 squid 注入节点提供数据支撑。

## 2. 测试环境

| 项目 | 说明 |
|---|---|
| Runner | `linux-amd64-cpu-4`（squid 代理注入节点，HTTP(S)_PROXY + MITM CA） |
| 矩阵 | ubuntu（cann:9.1.0-a3-ubuntu22.04-py3.12）、openeuler（cann:9.1.0-a3-openeuler24.03-py3.12） |
| 代理配置 | 本 workflow/脚本不配置任何代理，完全复用 pod 注入环境（PLAN.md 第 2 节原则） |
| 触发方式 | PR 创建/更新自动触发（`pull_request`）+ `workflow_dispatch` 手动 |

## 3. 执行记录

| Run | 触发 | 结果 | 用时 | 说明 |
|---|---|---|---|---|
| [35099852972](https://github.com/nv-action/vllm-benchmarks/actions/runs/35099852972) | PR #336 创建 | success | 5m11s | 首轮，暴露 4 个问题（见第 4 节） |
| [35166115990](https://github.com/nv-action/vllm-benchmarks/actions/runs/35166115990) | commit `43bc6442e` | success | 5m47s | 修复验证，pip/缓存/汇总已通过，但 PR 默认值表达式仍失效 |
| [35166661315](https://github.com/nv-action/vllm-benchmarks/actions/runs/35166661315) | commit `04668c280` | **success** | 5m40s | **最终验收 run，全部链路通过** |

## 4. 发现的问题与修复

| # | 问题 | 根因 | 修复 | 提交 |
|---|---|---|---|---|
| 1 | pip 链路全挂（ubuntu exit=127 `python3: command not found`；openeuler exit=1 `No module named pip`） | CANN 镜像缺 python3 / 缺 pip 模块 | 新增 `pip-bootstrap` 阶段，按包管理器补装 `python3 python3-pip` | `43bc6442e` |
| 2 | PR 事件下 `git-clone`/`obs-wget` 被关（环境快照显示 0） | PR 事件无 `inputs.*`，空字符串在 GitHub 表达式中 `== false` 为真 | 改为按 `github.event_name == 'pull_request'` 强制开启轻量链路 | `04668c280` |
| 3 | runs-on/cache 报 `CredentialsProviderError: Could not load credentials from any providers` | repo 未配置 `HW_OBS_AK`/`HW_OBS_SK` secrets（PLAN 第 7 节预判的风险成真） | 凭据缺失时整体跳过缓存链路并明确记录 `[skip]`，不视为失败 | `43bc6442e` |
| 4 | summary 只有 5 行数据、probes 未纳入汇总 | 两矩阵同名 `timings.tsv` 被 `merge-multiple` 覆盖 | 结果文件加 OS 后缀（`timings-ubuntu.tsv` 等），summary 逐文件展示 + 全矩阵合计 | `43bc6442e` |
| 5 | openeuler 节点 `https://github.com` HEAD 探测 30s 超时（exit 28），但 `git ls-remote` 1s 即通过 | squid MITM 下部分站点对 HEAD 响应异常，数据面正常 | probe 由 HEAD 改 GET | `43bc6442e` |

## 5. 最终结果（Run 35166661315）

### 5.1 各阶段耗时（phase / seconds / status，0 = 成功）

**timings-ubuntu.tsv**

| phase | seconds | status | category |
|---|---|---|---|
| apt-update | 5 | 0 | download |
| apt-install-zstd | 2 | 0 | download |
| pip-bootstrap | 11 | 0 | download |
| pip-install-small | 1 | 0 | download |
| pytorch-index-probe | 2 | 0 | download |
| git-ls-remote-github | 2 | 0 | download |
| git-shallow-clone-tiny | 3 | 0 | download |
| wget-small-object | 0 | 0 | download |
| obs-head-probe | 0 | 0 | probe |

**timings-openeuler.tsv**

| phase | seconds | status | category |
|---|---|---|---|
| dnf-update | 21 | 0 | download |
| pip-bootstrap | 2 | 0 | download |
| pip-install-small | 1 | 0 | download |
| pytorch-index-probe | 2 | 0 | download |
| git-ls-remote-github | 2 | 0 | download |
| git-shallow-clone-tiny | 2 | 0 | download |
| wget-small-object | 0 | 0 | download |
| obs-head-probe | 0 | 0 | probe |

**按类型汇总（双矩阵合计）**：`download runs=15 total=56s`、`probe runs=2 total=0s`；**失败阶段：无**。

### 5.2 直连探测（13 域名，双矩阵一致）

| url | status | seconds | 说明 |
|---|---|---|---|
| https://github.com | 200 | 0~1 | GET 正常（HEAD 曾超时，已改 GET） |
| https://raw.githubusercontent.com | 403 | 0 | 反爬预期响应 |
| repo.huaweicloud.com/repository/pypi/simple | 429 | 0 | 限流预期响应 |
| https://download.pytorch.org/whl/cpu/ | 200 | 0~1 | |
| repo.huaweicloud.com/ascend/repos/pypi | 301 | 0 | |
| https://modelscope.cn | 302 | 0 | |
| https://swr.cn-southwest-2.myhuaweicloud.com | 404 | 0 | 域名可达 |
| https://obs.cn-north-4.myhuaweicloud.com | 403 | 0~1 | 域名可达 |
| https://gh-proxy.test.osinfra.cn | 200 | 0 | |
| https://repo.huaweicloud.com | 301 | 0~1 | |
| https://mirrors.aliyun.com | 301 | 0 | |
| https://mirrors.tuna.tsinghua.edu.cn | 200 | 0 | |
| https://download.pytorch.org | 403 | 0~1 | |

### 5.3 上传链路

- **artifact**：upload-artifact（双矩阵）→ download-artifact（summary）拉回成功，数据完整；
- **cache（runs-on/cache → OBS）**：因 secrets 未配置按设计跳过，待补配后验证（见 7.1）；
- **skopeo copy / git push**：可选项，PR 事件下未开启（需 SWR/PAT 凭据）。

## 6. 验收结论（对照 PLAN.md 第 6 节）

| 验收项 | 结论 |
|---|---|
| workflow 在 squid runner 跑通、阶段级隔离 | ✅ 双矩阵全绿，无阶段失败 |
| 下载链路 A/B/C/E（系统包、pip、git、OBS 对象） | ✅ 全部 OK，耗时见 5.1 |
| 直连探测 I（13 域名） | ✅ 全部有响应，无超时 |
| 上传链路 F（artifact） | ✅ 上传/下载闭环 |
| 结果 TSV / summary 汇总 | ✅ 按矩阵分文件 + 全矩阵合计 + 失败清单 |
| G/H/J（cache OBS、skopeo、git push） | ⏸ 因凭据缺失跳过/未开启，见遗留事项 |

**结论：Squid 代理注入 runner 上的全部核心下载/上传链路验证通过，可以作为 CI runner 迁移的基线数据。**

## 7. 遗留事项

1. **OBS 缓存链路**：需在 repo 补配 `HW_OBS_AK` / `HW_OBS_SK` 两个 secrets，然后手动 `workflow_dispatch` 一次即可完成 cache save→restore 命中闭环验证；
2. **skopeo copy / git push**：需 SWR / PAT_TOKEN 凭据时用 workflow_dispatch 勾选对应开关执行；
3. **大载荷验证**：当前为 mock 小载荷（1MB），pip 大包（`TEST_SQUID_PIP_MODELSCOPE=1`）与 modelscope 模型下载默认关闭，可在 dispatch 时开启做真实大包链路压测；
4. **体验数据**：squid MITM 下 `dnf-update`(21s) 明显慢于 `apt-update`(5s)，`pip-bootstrap` 在 ubuntu 首次 21s（属镜像缺件，非代理问题），后续迁移评估时可参考。
