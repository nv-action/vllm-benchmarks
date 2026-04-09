# Main2Main 变更分析

## 0. 分析范围说明

本文严格按用户指定区间分析以下两个提交之间的**全部累计修改**，不逐个 commit 拆分：

- 起始提交：`5c867bde59959198e8177b48a1ae519885ffbd4e`
- 结束提交：`023712788d9eea7addf791adcff21d876e858598`

执行命令等价于：

```bash
git diff 5c867bde59959198e8177b48a1ae519885ffbd4e 023712788d9eea7addf791adcff21d876e858598
```

分析目标是这整个 commit range 的**总体效果、文件职责、使用方式和参数合同**，而不是逐条 commit review。

### 变更规模

| 项目 | 值 |
|---|---|
| 变更文件数 | 56 |
| 修改 | 18 |
| 新增 | 15 |
| 删除 | 22 |
| 重命名 | 1 |
| 插入/删除 | 8739 插入, 8923 删除 |

---

## 1. 当前修改总体做了什么

这批修改不是零碎修补，而是一次完整的 `main2main` 控制面迁移。核心变化可以概括为 6 点：

1. **把本地守护进程式控制面，迁移为 GitHub Actions 原生控制面**
   - 删除了本地 `service_main.py + main2main_orchestrator.py + mcp_server.py + state_store.py + terminal_worker.py + github_adapter.py` 这一整套服务栈。
   - 用 GitHub Actions 工作流加 PR 评论状态取代本地进程轮询、状态文件、MCP SSE 服务和 systemd 部署。

2. **用 PR 结构化评论替代本地状态文件**
   - 新状态源由两类评论组成：
   - `main2main-register`
   - `main2main-state:v1`
   - `main2main-state` 成为唯一在线状态源，记录 `phase`、`status`、`dispatch_token`、`e2e_run_id`、`fix_run_id`、`bisect_run_id` 等。

3. **把单一大工作流拆成“调度 / 协调 / 终态处理 / bisect”四块**
   - `schedule_main2main_auto.yaml`：detect、phase2 fix、phase3 prepare、phase3 finalize。
   - `schedule_main2main_reconcile.yaml`：定时或手工 reconcile `waiting_e2e` / `waiting_bisect`。
   - `dispatch_main2main_terminal.yaml`：`make_ready` / `manual_review`。
   - `dispatch_main2main_bisect.yaml`：独立 bisect，并通过 reconcile 回流主状态机。

4. **把状态机逻辑收敛到一个可 CLI 调用的 helper 脚本**
   - 新增 `.github/workflows/scripts/main2main_ci.py`。
   - 这个脚本把“评论解析、状态转移、stale guard、run 选择、dispatch token 轮转、PR 上下文读取、workflow 恢复”等逻辑统一封装。

5. **把 bisect 和日志分析工具体系化**
   - `ci_log_summary.py` 扩展为真正的 CI 日志摘要入口，能输出 `summary/json/llm-json/bisect-json`。
   - `tools/bisect_helper.py` 从硬编码环境规则改为直接读取 CI workflow，动态提取 runner、image、install step、runtime env，并新增 `result-json` 聚合能力。
   - `tools/bisect_vllm.sh` 从 workflow 私有脚本提升为 repo 级工具，支持 batch mode、summary 输出、repo auto-detect、超时参数与更稳健的 commit 解析。

6. **同步更新了技能文档、设计文档和测试体系**
   - `.agents` / `.claude` 技能文档改成以 `ci_log_summary.py` 为入口。
   - 新增 workflow-native 设计与实施计划文档。
   - 删除旧 orchestrator/service 测试，新增 `test_main2main_ci.py`、workflow contract 测试、bisect runtime env 测试与 bisect result 聚合测试。

---

## 2. 架构层面的前后对比

| 维度 | 旧架构 | 新架构 |
|---|---|---|
| 控制平面 | 本地 Python 服务 + systemd + MCP SSE | GitHub Actions 原生工作流 |
| 状态存储 | 本地 JSON 文件 | PR 评论 `main2main-state:v1` |
| 调度方式 | 本地轮询 `gh` | `schedule` + `workflow_dispatch` |
| 终态动作 | 本地 terminal worker | 独立 terminal workflow |
| bisect 回调 | 由本地控制面接收与推进 | bisect workflow 结束后触发 reconcile |
| stale 防护 | 本地状态 + run 轮询 | `dispatch_token` + PR/head/comment 一致性检查 |
| 运维方式 | 需要部署服务进程 | 仅依赖 GitHub Actions、PAT、Claude 网关 |

### 旧架构职责映射到新架构

| 旧文件/模块 | 原职责 | 新落点 |
|---|---|---|
| `main2main_orchestrator.py` | 状态机、reconcile、fixup dispatch | `.github/workflows/scripts/main2main_ci.py` + `schedule_main2main_reconcile.yaml` |
| `github_adapter.py` | `gh` 调用包装 | `main2main_ci.py` 内的 `_gh/_gh_json/_gh_api_json` |
| `service_main.py` | 后台 poll loop + MCP SSE 服务启动 | 不再需要，交给 Actions `schedule` |
| `state_store.py` | 本地 JSON 持久化 | PR 评论状态 |
| `terminal_worker.py` | 异步 issue 创建与 terminal 行为 | `dispatch_main2main_terminal.yaml` |
| `mcp_server.py` | 对外暴露 orchestrator 工具 | 不再保留生产路径 |
| `deploy/systemd/*` | 服务部署资产 | 不再保留 |

---

## 3. 关键执行文件详解

这一节只详细展开“真正可执行、可调度、带参数的核心文件”。其余文件会在后面的逐文件清单中覆盖。

### 3.1 `.github/workflows/schedule_main2main_auto.yaml`

**作用**

- 主工作流。
- 负责四种模式：
  - `detect`
  - `fix_phase2`
  - `fix_phase3_prepare`
  - `fix_phase3_finalize`

**触发方式**

1. 定时触发：

```bash
# 每天 UTC 14:00
schedule:
  - cron: '0 14 * * *'
```

2. 手工触发：

```bash
gh workflow run schedule_main2main_auto.yaml \
  --repo nv-action/vllm-benchmarks \
  -f mode=fix_phase2 \
  -f pr_number=188 \
  -f dispatch_token=<token>
```

**workflow_dispatch 输入参数**

| 参数 | 作用 | 何时使用 |
|---|---|---|
| `mode` | 执行模式：`detect`、`fix_phase2`、`fix_phase3_prepare`、`fix_phase3_finalize` | 所有手工触发 |
| `target_commit` | 指定 vLLM 目标提交；为空时使用 upstream `main` HEAD | `detect` |
| `pr_number` | 目标 main2main PR 号 | 所有 fix 模式 |
| `dispatch_token` | stale guard；要求与当前 state comment 完全一致 | 所有 fix 模式 |
| `bisect_run_id` | 指定 bisect workflow run id；为空时从 state 读取 | `fix_phase3_finalize` |

**主要 job**

| Job | 作用 |
|---|---|
| `detect-and-adapt` | 检测 upstream vLLM 漂移、调用 Claude 生成初始适配、创建 draft PR、写入 register/state 评论 |
| `fix-phase2` | 对首次 E2E 失败做 Claude 修复；有变更则推送并回到 `waiting_e2e` |
| `fix-phase3-prepare` | 生成 bisect payload，触发 bisect workflow，并把状态置为 `waiting_bisect` |
| `fix-phase3-finalize` | 下载 bisect 结果，做 bisect-guided 修复；无变更则转 `manual_review_pending` 并触发 terminal workflow |

**关键环境变量**

| 变量 | 作用 |
|---|---|
| `UPSTREAM_REPO` | 控制仓库，固定为 `nv-action/vllm-benchmarks` |
| `FORK_OWNER` | 工作分支所在 fork owner |
| `CONTROL_REPO_DIR` | 控制仓库 checkout 路径 |
| `WORK_REPO_DIR` | 可写工作仓 checkout 路径 |
| `MAIN2MAIN_MODEL` | Claude 模型名 |
| `GH_TOKEN` | 用于 `gh` 操作的 PAT |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | Claude gateway |

**这次修改带来的关键语义变化**

- fix 模式不再接收长输入合同，例如 `branch/head_sha/run_id/run_url/conclusion/phase/old_commit/new_commit`。
- fix job 统一只接收 `pr_number + dispatch_token (+ bisect_run_id)`，其余上下文从 PR 评论状态里读取。
- phase3 不是直接从 bisect workflow 回调 finalize，而是先回到 reconcile，再由 reconcile 决定是否 dispatch finalize。

### 3.2 `.github/workflows/schedule_main2main_reconcile.yaml`

**作用**

- 新的控制中枢。
- 定期扫描所有带 `main2main` label 的 open PR。
- 负责：
  - 初始化缺失的 `main2main-state` 评论
  - 从 `statusCheckRollup` 或 `gh run list` 解析当前 E2E 结果
  - 根据当前 `phase/status` 决定下一跳
  - 在 `waiting_bisect` 状态下，发现 bisect 完成但 finalize 未执行时继续推进

**触发方式**

```bash
gh workflow run schedule_main2main_reconcile.yaml \
  --repo nv-action/vllm-benchmarks \
  -f pr_number=188
```

**输入参数**

| 参数 | 作用 |
|---|---|
| `pr_number` | 可选；为空时扫描全部 open main2main PR，只填时只 reconcile 指定 PR |

**关键点**

- 这是替代旧 `poll_loop` 的文件。
- 任何“等待某个 run 完成后再推进”的逻辑，现在都不依赖常驻服务，而依赖它的 schedule。

### 3.3 `.github/workflows/dispatch_main2main_terminal.yaml`

**作用**

- 处理终态动作。
- 替代旧 `terminal_worker.py`。
- 两类动作：
  - `make_ready`
  - `manual_review`

**触发方式**

```bash
gh workflow run dispatch_main2main_terminal.yaml \
  --repo nv-action/vllm-benchmarks \
  -f action=manual_review \
  -f pr_number=188 \
  -f dispatch_token=<token> \
  -f terminal_reason=phase3_no_changes
```

**输入参数**

| 参数 | 作用 |
|---|---|
| `action` | `make_ready` 或 `manual_review` |
| `pr_number` | 目标 PR |
| `dispatch_token` | stale guard；要求与 state 中当前 token 一致 |
| `terminal_reason` | 进入 manual review 的原因，例如 `phase3_no_changes`、`workflow_error` |

**内部步骤**

| 步骤 | 作用 |
|---|---|
| `Load state comment` | 调用 `load-phase-context`，把 PR、registration、state 统一加载出来 |
| `Make PR ready` | `gh pr ready`，把 draft PR 转为 ready |
| `Collect manual review context` | 读取 `e2e_run_id/fix_run_id/bisect_run_id`，调用 `ci_log_summary.py` 生成分析上下文 |
| `Generate manual review issue body with Claude` | 生成 issue 正文 |
| `Create manual review issue` | 创建 issue |
| `Update state comment` | 把状态更新为 `ready` 或 `manual_review` |

**失败恢复**

- 如果 terminal workflow 自己失败，会调用 `prepare-workflow-error-recovery`。
- 在 retry 预算内自动重新 dispatch 自己。
- 超过预算后只留下状态给人工接管，不再无限重试。

### 3.4 `.github/workflows/dispatch_main2main_bisect.yaml`

**作用**

- 执行 vLLM 提交区间 bisect。
- 既支持独立使用，也支持被 main2main 调用。

**触发方式**

```bash
gh workflow run dispatch_main2main_bisect.yaml \
  --repo nv-action/vllm-benchmarks \
  -f caller_type=main2main \
  -f caller_run_id=24000000000 \
  -f good_commit=<good> \
  -f bad_commit=<bad> \
  -f test_cmd='pytest -sv tests/e2e/singlecard/test_sampler.py::test_x' \
  -f main2main_pr_number=188 \
  -f main2main_dispatch_token=<token>
```

**输入参数**

| 参数 | 作用 |
|---|---|
| `caller_type` | `standalone` 或 `main2main` |
| `caller_run_id` | 调用方 run id，用于追踪和 run title |
| `good_commit` | 已知好提交 |
| `bad_commit` | 已知坏提交 |
| `test_cmd` | 失败测试命令，支持分号分隔批量命令 |
| `main2main_pr_number` | callback 上下文，仅 `main2main` 模式使用 |
| `main2main_dispatch_token` | callback stale guard 上下文，仅 `main2main` 模式使用 |

**主要 job**

| Job | 作用 |
|---|---|
| `set-params` | 调用 `tools/bisect_helper.py batch-matrix` 生成矩阵 |
| `bisect` | 分组执行 bisect |
| `upload-all-results` | 下载各组 artifact，汇总成 `bisect_summary.md` 和 `bisect_result.json` |
| `callback-main2main` | 当 `caller_type=main2main` 时，改为触发 reconcile，而不是直接 finalize |

**这次修改最重要的变化**

- 原来的 `bisect_vllm.yaml` 被替换为 `dispatch_main2main_bisect.yaml`。
- callback 路径不再直接调用 `fix_phase3_finalize`，而是通过 `schedule_main2main_reconcile.yaml` 回流主状态机。
- 产物从单一 summary 扩展为 `bisect_result.json`，为 phase3 finalize 提供机器可读结果。

### 3.5 `.github/workflows/scripts/main2main_ci.py`

**作用**

- 这是 workflow-native 架构的核心 helper。
- 所有 GitHub Actions job 都通过它完成状态读写、评论渲染、guard 检查、reconcile 决策、bisect payload 生成。

**核心数据结构**

| 数据结构 | 作用 |
|---|---|
| `PrMetadata` | 从 PR body 里抽取 `old_commit/new_commit` |
| `RegistrationMetadata` | `main2main-register` 评论内容 |
| `Main2MainState` | 运行时在线状态 |
| `GuardResult` | stale/一致性检查结果 |
| `ReconcileDecision` | reconcile 产出的下一步动作 |
| `FixupOutcome` | fix job 的结果表示 |
| `MarkerComment` | 包含 comment id 和 body 的辅助结构 |
| `PhaseContext` | 将 PR + state + register + comment id 打包，供 job 复用 |

**`Main2MainState` 字段含义**

| 字段 | 作用 |
|---|---|
| `pr_number` | PR 编号 |
| `branch` | PR head branch |
| `head_sha` | 当前 PR head SHA |
| `old_commit` | 已适配的旧 vLLM 提交 |
| `new_commit` | 新目标 vLLM 提交 |
| `phase` | `2`、`3`、`done` |
| `status` | `waiting_e2e`、`fixing`、`waiting_bisect`、`ready`、`manual_review`、`manual_review_pending`、`error` 等 |
| `dispatch_token` | 当前活跃动作 token，用于 stale guard |
| `e2e_run_id` | 当前关联 E2E run |
| `fix_run_id` | 当前 fix workflow run |
| `bisect_run_id` | 当前 bisect workflow run |
| `terminal_reason` | 终态原因 |
| `workflow_error_count` | 当前 workflow 的重试计数 |
| `last_transition` | 最近一次状态转移名称 |
| `updated_at` | 更新时间 |
| `updated_by` | 最后更新者，通常是 `workflow/job` 名 |

**CLI 子命令及参数**

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `mint-dispatch-token` | 生成新 token | 无 |
| `state-read` | 从 state comment 解析 JSON | `--comment-file` |
| `state-write` | 把 state JSON 渲染回 comment | `--json-file` |
| `registration-read` | 解析 register comment | `--comment-file` |
| `registration-write` | 渲染 register comment | `--json-file` |
| `state-init-from-register` | 由 register 初始化 state | `--comment-file --dispatch-token [--updated-at] [--updated-by]` |
| `guard-check` | 校验 phase/status/token | `--state-file [--expected-phase] [--expected-status] [--dispatch-token]` |
| `pr-consistency-check` | 校验 branch/head_sha | `--state-file --branch --head-sha` |
| `registration-consistency-check` | 校验 state 和 register 一致 | `--state-file --registration-file` |
| `reconcile-decision` | 纯函数方式生成 reconcile 决策 | `--state-file [--e2e-run-file] [--merge-state-status] [--mergeable] [--bisect-finished] [--finalize-missing]` |
| `reconcile-pr` | 对指定 PR 执行完整 reconcile | `--repo --pr-number` |
| `select-e2e-run` | 从 run 列表里选择匹配当前 head 的 E2E run | `--runs-file --head-sha` |
| `apply-fix-result` | 根据 fix 结果推进 phase/head | `--state-file --result [changes_pushed/no_changes] [--new-head-sha]` |
| `json-get` | 从 JSON 文件读取字段 | `--json-file --field` |
| `upsert-pr-phase-section` | 更新 PR body 中 Phase 章节 | `--body-file --heading --content-file --output-file` |
| `prepare-detect-artifacts` | 生成 detect 阶段的 register/state 初始产物 | `--pr-number --branch --head-sha --old-commit --new-commit --dispatch-token --state-json-out --register-json-out --state-comment-out --register-comment-out` |
| `prepare-fix-transition` | 生成 phase fix 后的新 state/register/comment | `--state-file --result --fix-run-id --last-transition --updated-by ...` |
| `prepare-bisect-payload` | 用 state + ci analysis 生成 bisect 输入 | `--state-file --ci-analysis-file [--payload-json-out]` |
| `prepare-fixing-state` | 把 state 更新为 fixing | `--state-file --fix-run-id --last-transition --updated-by --state-json-out --state-comment-out` |
| `prepare-waiting-bisect` | 写入 `waiting_bisect` 状态 | `--state-file --bisect-run-id --fix-run-id --last-transition --updated-by --state-json-out --state-comment-out` |
| `prepare-manual-review-pending` | phase3 无变更时先写 pending 状态 | `--state-file --terminal-reason --fix-run-id --last-transition --updated-by ...` |
| `prepare-workflow-error-action` | 给当前失败 job 计算下一步 action | `--state-file --next-dispatch-token --max-retries --terminal-reason --retry-transition --terminal-transition --updated-by ...` |
| `prepare-workflow-error-recovery` | 失败恢复版，内部自动 mint 新 token | `--state-file --max-retries --terminal-reason --retry-transition --terminal-transition --updated-by ...` |
| `extract-pr-comments` | 从 issue comments 中抽取 state/register 评论与 comment id | `--comments-file [--state-comment-out] [--state-id-out] [--register-comment-out] [--register-id-out]` |
| `select-bisect-run-id` | 在 run 列表中通过 `caller_run_id + dispatch_token` 识别 bisect run | `--runs-file --caller-run-id --dispatch-token` |
| `load-phase-context` | 最常用入口；一次性加载 PR/state/register/id/context | `--repo --pr-number [--expected-phase] [--expected-status] [--allowed-statuses ...] [--dispatch-token] [--pr-json-out] [--state-json-out] [--registration-json-out] [--state-id-out] [--register-id-out] [--context-json-out]` |

**典型用法**

```bash
python3 .github/workflows/scripts/main2main_ci.py load-phase-context \
  --repo nv-action/vllm-benchmarks \
  --pr-number 188 \
  --expected-phase 3 \
  --allowed-statuses fixing waiting_bisect \
  --dispatch-token <token> \
  --state-json-out /tmp/main2main_state.json \
  --registration-json-out /tmp/main2main_register.json \
  --context-json-out /tmp/main2main_ctx.json
```

### 3.6 `.github/workflows/scripts/ci_log_summary.py`

**作用**

- 统一的 CI 日志摘要入口。
- 可以从本地 log 文件或 GitHub Actions run id 读取数据。
- 负责抽取：
  - failed test files
  - failed test cases
  - 去重后的 root cause errors
  - good/bad commit
  - bisect payload

**命令行参数**

| 参数 | 作用 |
|---|---|
| `--log-file` | 分析本地日志文件，与 `--run-id` 二选一 |
| `--run-id` | 分析 GitHub Actions run |
| `--repo` | `--run-id` 模式使用的仓库，默认 `nv-action/vllm-benchmarks` |
| `--mode` | `ut` 或 `e2e`，用于 summary 标题 |
| `--step-name` | summary 中显示的 step 名 |
| `--format` | `summary`、`json`、`llm-json`、`bisect-json` |
| `--output` | 输出文件；不填则 stdout |

**输出格式**

| 格式 | 用途 |
|---|---|
| `summary` | 人类可读 Markdown 摘要 |
| `json` | 完整结构化结果 |
| `llm-json` | 给技能/Claude 使用的压缩 JSON |
| `bisect-json` | 为 phase3 准备 representative test cases 和 `test_cmd` |

**这次修改的重点**

- 替代了旧的 `extract_failures.py` 与技能目录下的 `extract_and_analyze.py`。
- 支持 repo override、EOF retry、representative test case 选择，以及更强的 traceback / summary 提取。

### 3.7 `tools/bisect_helper.py`

**作用**

- 为 bisect workflow 和 `tools/bisect_vllm.sh` 提供支持。
- 新版本最大的特点是：**不再把环境配置硬编码在脚本里，而是直接读取 `_e2e_test.yaml` / `_unit_test.yaml` 等 workflow。**

**子命令**

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `batch-matrix` | 把分号分隔测试命令转成 GitHub Actions matrix | `--test-cmds --output-format` |
| `get-commit` | 从 workflow yaml 抽取 vLLM commit | `--yaml-path [--ref]` |
| `report` | 生成 Markdown bisect 报告 | `--good-commit --bad-commit --first-bad --test-cmd --total-steps --total-commits [--first-bad-info] [--first-bad-info-file] [--skipped] [--log-file] [--summary-output]` |
| `vllm-location` | 通过 `pip show` 找到 editable/local vllm 安装位置 | 无 |
| `vllm-install` | 为某个测试命令返回 vllm 安装命令 | `--test-cmd` |
| `ascend-install` | 为某个测试命令返回 vllm-benchmark 安装命令 | `--test-cmd` |
| `result-json` | 汇总各组 bisect summary，输出机器可读 `bisect_result.json` | `--caller-type --caller-run-id --bisect-run-id --good-commit --bad-commit --test-cmd --results-dir [--summary-file] [--output]` |

**关键内部能力**

| 能力 | 说明 |
|---|---|
| repo root 自动发现 | 支持从仓库根、环境变量、脚本路径推断 |
| runtime env manifest | 从现有 workflow 提取 runner/image/install/runtime env |
| group 聚合 | 把不同测试类型拆成 `ut/e2e-singlecard/e2e-4cards/e2e-310p-*` 等组 |
| bisect 结果聚合 | 输出 `success/ambiguous/partial_success/failed` |

### 3.8 `tools/bisect_vllm.sh`

**作用**

- 真正执行 `git bisect` 的 shell 工具。
- 同时支持本地手工使用和 GitHub Actions 中的批量执行。

**主要参数**

| 参数 | 作用 |
|---|---|
| `--test-cmd` | 单个失败测试命令 |
| `--test-cmds-file` | 存放分号分隔测试命令的文件，批量模式 |
| `--good` | 已知好提交 |
| `--bad` | 已知坏提交 |
| `--vllm-repo` | vllm 仓库路径，默认自动探测 |
| `--ascend-repo` | vllm-benchmark 仓库路径，默认自动探测 |
| `--env` | 额外环境变量 |
| `--fetch-depth` | bisect 前拉取历史的深度，`0` 表示完整历史 |
| `--step-timeout` | 单步超时，默认 1800 秒 |
| `--total-timeout` | 总超时，默认 20400 秒 |
| `--summary-output` | Markdown 总结输出文件 |

**这次修改的重点**

- 从 `.github/workflows/scripts/` 移到 `tools/`，成为可直接本地复用的 repo 工具。
- 去掉旧的 `--no-fetch` 路径，统一成可选 `--fetch-depth`。
- 默认 timeout 更长，batch 模式与 summary output 更成熟。

---

## 4. 逐文件清单

下面按类别列出**全部 56 个变更文件**。`使用方法` 一栏尽量说明“谁会调用它、如何调用、是否有参数”。对已经在第 3 节展开的大文件，这里用“见 3.x”避免重复。

### 4.1 技能与提示词文档

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `.agents/skills/main2main-error-analysis/SKILL.md` | M | 把 CI 故障分析技能从“自带脚本自动提取”改成“依赖仓库内 `ci_log_summary.py` 先生成结构化摘要，再人工追根因”。 | 给 agent 使用的技能入口文档。 | 无运行参数；文中要求提供 `RUN_ID`，并生成 `/tmp/ci_analysis.json`。 |
| `.agents/skills/main2main-error-analysis/scripts/extract_and_analyze.py` | D | 旧的技能私有日志抽取脚本，负责下载日志、抽取失败用例和错误。 | 旧用法是 `python3 scripts/extract_and_analyze.py --run-id ...`。 | `--run-id`、`-o/--output` 等；现已被 `ci_log_summary.py` 替代。 |
| `.agents/skills/main2main/SKILL.md` | M | 原主技能补充了“修复完成后统一替换 good/bad commit 引用”的操作步骤。 | Claude/agent 根据技能说明执行适配。 | 无程序参数；核心输入是 good/bad commit。 |
| `.agents/skills/main2main_v2/SKILL.md` | A | 新版主技能入口，把能力拆成 `proactive-upgrade` 和 `error-analysis` 两种场景，并显式声明不做 git/PR 操作。 | 作为 workflow 或人工调用的技能入口。 | 无运行参数；根据用户场景决定读取哪个子文档。 |
| `.agents/skills/main2main_v2/error-analysis.md` | A | v2 的 CI 故障诊断流程文档，指导如何先用 `ci_log_summary.py`，再做 commit 关联、生成报告、修复并输出 summary。 | 人工或 agent 按步骤执行。 | 核心输入：`RUN_ID`、`GOOD_COMMIT`、`BAD_COMMIT`。 |
| `.agents/skills/main2main_v2/evals/evals.json` | R | 评估配置占位文件，从 `.claude/skills/main2main/evals/evals.json` 重命名过来。 | 供未来技能评测使用。 | 无。 |
| `.agents/skills/main2main_v2/proactive-upgrade.md` | A | v2 的主动升级工作流说明，面向“分析 upstream 改动并主动适配”。 | 人工或 agent 按步骤执行。 | 核心输入：`old_commit`、`new_commit`。 |
| `.agents/skills/main2main_v2/reference/error-patterns.md` | A | 常见错误模式参考库，给 CI 故障诊断阶段提供“错误模式 -> 修复方式”的快速映射。 | 被 `main2main_v2` 技能引用。 | 无运行参数；输入是具体错误类型。 |
| `.claude/skills/main2main/SKILL.md` | M | Claude 侧主技能入口同步对齐 v2 结构，删去冗长映射表，强调“只产出代码与 summary，不做 git/PR”。 | 被 Claude Code Action 读取。 | 无。 |
| `.claude/skills/main2main/error-analysis.md` | M | Claude 侧 CI 故障诊断文档同步切换到 `ci_log_summary.py`。 | 被 `schedule_main2main_auto.yaml` phase3 prompt 间接使用。 | 依赖 `RUN_ID`、本地 vLLM 仓库、good/bad commit。 |
| `.claude/skills/main2main/proactive-upgrade.md` | M | Claude 侧主动升级文档内容被扩充，增加关键区域、命令模板和 commit 引用替换说明。 | 被 detect/适配 prompt 使用。 | 依赖 `old_commit/new_commit`。 |

### 4.2 Workflow 与执行脚本

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `.github/workflows/_e2e_test.yaml` | M | 主 E2E 工作流的文案、safe.directory 和部分 runner 细节更新，成为 `bisect_helper.py` 读取的事实来源。 | GitHub Actions 正常触发；也被 `bisect_helper.py` 解析。 | 无新增输入；但其中 `job/step/env` 成为 bisect runtime 配置来源。 |
| `.github/workflows/_pre_commit.yml` | M | checkout 名称与 safe.directory 路径从旧仓库名切到当前仓库名。 | 预提交检查工作流。 | 无。 |
| `.github/workflows/_unit_test.yaml` | M | unit test 工作流的 repo 名称与 safe.directory 路径同步到当前仓库。 | 单元测试工作流；也被 `bisect_helper.py` 读取。 | 无新增输入。 |
| `.github/workflows/bisect_vllm.yaml` | D | 旧 bisect workflow。 | 旧用法为 workflow_dispatch，现被 `dispatch_main2main_bisect.yaml` 替代。 | 旧输入包括 `good_commit/bad_commit/test_cmd` 等。 |
| `.github/workflows/bot_merge_conflict.yaml` | M | 增补了 `contents/issues/pull-requests` permissions，避免机器人在 merge conflict 场景下权限不足。 | 自动化冲突处理工作流。 | 无。 |
| `.github/workflows/dispatch_main2main_bisect.yaml` | A | 新 bisect workflow。 | 见 3.4。 | `caller_type`、`caller_run_id`、`good_commit`、`bad_commit`、`test_cmd`、`main2main_pr_number`、`main2main_dispatch_token`。 |
| `.github/workflows/dispatch_main2main_terminal.yaml` | A | 新 terminal workflow。 | 见 3.3。 | `action`、`pr_number`、`dispatch_token`、`terminal_reason`。 |
| `.github/workflows/labled_doctest.yaml` | M | 仓库名与工作目录注释同步更新。 | 带 label 的 doctest workflow。 | 无。 |
| `.github/workflows/labled_download_model.yaml` | M | safe.directory 改为 `$GITHUB_WORKSPACE`。 | 带 label 的模型下载 workflow。 | 无。 |
| `.github/workflows/main2main_auto.yaml` | D | 旧主工作流，fixup 合同字段很长，仍依赖外部 orchestrator 提供上下文。 | 旧用法 `gh workflow run main2main_auto.yaml -f mode=fixup ...`。 | 旧输入过长：`branch/head_sha/run_id/run_url/conclusion/phase/old_commit/new_commit/dispatch_token`。 |
| `.github/workflows/schedule_main2main_auto.yaml` | A | 新主工作流。 | 见 3.1。 | 见 3.1。 |
| `.github/workflows/schedule_main2main_reconcile.yaml` | A | 新 reconcile 工作流。 | 见 3.2。 | `pr_number`。 |
| `.github/workflows/scripts/bisect_helper.py` | D | 旧版 bisect helper，环境规则多为硬编码。 | 旧用法为 workflow 内或 shell 脚本内子命令调用。 | 旧子命令：`detect-env/get-commit/report/...`。 |
| `.github/workflows/scripts/bisect_vllm.sh` | D | 旧版 bisect shell，主要服务于 workflow 私有路径。 | 旧用法为 `.github/workflows/scripts/bisect_vllm.sh ...`。 | `--good`、`--bad`、`--test-cmd`、`--no-fetch` 等。 |
| `.github/workflows/scripts/ci_log_summary.py` | M | 扩展成统一日志摘要工具，支持 `llm-json` 与 `bisect-json`。 | 见 3.6。 | 见 3.6。 |
| `.github/workflows/scripts/extract_failures.py` | D | 旧的 workflow 私有失败测试提取脚本。 | 旧用法 `python3 .github/workflows/scripts/extract_failures.py --repo ... --run-id ...`。 | `--repo`、`--run-id`、`--only-hour`、`--check-duplicate`。 |
| `.github/workflows/scripts/main2main_ci.py` | A | 新控制面 helper。 | 见 3.5。 | 见 3.5。 |

### 4.3 文档、设计与实施计划

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `benchmarks/README.md` | M | 将 README 中 GitHub 链接从旧仓库地址切到 `nv-action/vllm-benchmark`。 | 给使用者阅读。 | 无。 |
| `docs/superpowers/plans/2026-03-12-main2main-mcp-service.md` | M | 旧 MCP service 实施计划，仅做补充说明/格式修正，保留历史方案。 | 历史实施计划，不再是当前生产实现。 | 无运行参数。 |
| `docs/superpowers/plans/2026-04-07-main2main-workflow-native-orchestration.md` | A | 新架构的实施计划文档，定义从本地服务迁移到 workflow-native 的步骤。 | 设计/实施参考文档。 | 无运行参数；关注文件清单、迁移步骤和验收项。 |
| `docs/superpowers/specs/2026-03-12-main2main-mcp-service-design.md` | M | 旧 MCP service 设计稿，仅做格式/注释细修，保留历史背景。 | 历史设计文档。 | 无。 |
| `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md` | A | 新控制面的正式设计文档，定义状态评论 schema、workflow 合同和迁移目标。 | 当前这批改动的总设计参考。 | 无运行参数；文中定义了 state 字段和 workflow 输入合同。 |

### 4.4 被删除的旧控制面与部署资产

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `deploy/systemd/orchestrator.env.example` | D | 旧 systemd 环境变量示例。 | 部署时填入 `/etc/.../orchestrator.env`。 | `GITHUB_TOKEN`、`ANTHROPIC_*`、`STATE_PATH`、`POLL_INTERVAL`、`REPO`、`MCP_HOST`、`MCP_PORT`。 |
| `deploy/systemd/vllm-benchmarks-orchestrator.service` | D | 旧 systemd service 单元，负责启动 `service_main.py`。 | `systemctl enable/start`。 | `EnvironmentFile`、`ExecStart`、`TimeoutStopSec` 等 systemd 参数。 |
| `github_adapter.py` | D | 旧 `gh` 调用封装层，包装 PR 列表、run 查询、dispatch、mark ready、issue 创建等。 | 由 orchestrator/service 导入使用。 | 面向方法参数，不是 CLI：`repo`、`pr_number`、`dispatch_token` 等。 |
| `main2main_orchestrator.py` | D | 旧主状态机实现，负责解析 PR 评论、等待 E2E、决定下一动作、更新本地 state。 | 可直接 CLI 运行，也被 service 调用。 | 内部数据结构包括 `Main2MainState`、`RegistrationMetadata`；旧 CLI 支持 register/reconcile/run-once 等。 |
| `mcp_server.py` | D | 旧 MCP 包装层，把 orchestrator 暴露为 6 个 MCP tool。 | 由 `service_main.py` 启动 SSE 服务。 | 工具入参包括 `repo`、`label`、`pr_number`。 |
| `service_main.py` | D | 旧服务入口，启动 poll loop、terminal worker 和 MCP SSE server。 | `python service_main.py` 或 systemd 启动。 | 环境变量：`STATE_PATH`、`REPO`、`POLL_INTERVAL`、`MCP_HOST`、`MCP_PORT`。 |
| `state_store.py` | D | 旧 JSON 持久化封装。 | 由 orchestrator 与 terminal worker 调用。 | 无 CLI；核心对象 `JsonStore(path)`。 |
| `terminal_worker.py` | D | 旧异步 terminal worker，处理 manual review issue 生成与状态更新。 | 由 `service_main.py` 中队列驱动。 | `TerminalJob` 字段和 worker 初始化回调参数。 |

### 4.5 测试文件

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `tests/main2main/test_bisect_helper_runtime_env.py` | A | 验证 `bisect_helper.py` 是否从 `_e2e_test.yaml/_unit_test.yaml` 正确提取 runner、image、install/runtime env。 | `pytest -q tests/main2main/test_bisect_helper_runtime_env.py` | 无额外参数。 |
| `tests/main2main/test_deploy_assets.py` | D | 验证旧 systemd 资产是否存在并包含必要变量。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_extract_and_analyze_contract.py` | M | 现在主要验证 `ci_log_summary.py` 的 repo override、bisect-json、good/bad commit 提取、以及 `bisect_helper.py result-json`。 | `pytest -q tests/main2main/test_extract_and_analyze_contract.py` | 无额外参数；通过 monkeypatch 构造输入。 |
| `tests/main2main/test_fixup_dispatch_contract.py` | D | 验证旧 orchestrator 会用长合同字段 dispatch `main2main_auto.yaml`。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_github_adapter.py` | D | 验证 `GitHubCliAdapter` 的 label 查询。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_main2main_ci.py` | A | 新核心测试，覆盖 comment round-trip、load-phase-context、reconcile 决策、phase 转移、workflow error recovery、bisect payload 和 bootstrap 恢复。 | `pytest -q tests/main2main/test_main2main_ci.py` | 无 CLI 参数；通过 fixture/临时文件构造。 |
| `tests/main2main/test_main2main_workflow_contract.py` | M | 重新对齐新 workflow 体系，验证短输入合同、split mode、phase3 bisect、terminal retry、working-directory 和 bash 语法。 | `pytest -q tests/main2main/test_main2main_workflow_contract.py` | 无。 |
| `tests/main2main/test_mcp_server.py` | D | 验证旧 MCP server 的六个 tool 和 health schema。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_orchestrator.py` | D | 旧最大测试文件，覆盖 orchestrator 状态机、CLI、run once、GitHub adapter、wait E2E、fixup outcome 等。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_service_main.py` | D | 验证旧 poll loop、service lock 和 SSE server 包装。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_state_store.py` | D | 验证旧 `JsonStore`。 | 旧 `pytest` 文件。 | 无。 |
| `tests/main2main/test_terminal_worker.py` | D | 验证旧 terminal worker 队列、幂等性、issue 创建和状态更新。 | 旧 `pytest` 文件。 | 无。 |

### 4.6 工具与杂项

| 文件 | 状态 | 文件在做什么 | 使用方法 | 关键参数/输入 |
|---|---|---|---|---|
| `tools/bisect_helper.py` | A | 新 repo 级 bisect helper。 | 见 3.7。 | 见 3.7。 |
| `tools/bisect_vllm.sh` | A | 新 repo 级 bisect shell。 | 见 3.8。 | 见 3.8。 |
| `tools/format_contributors.py` | M | 默认 repo 参数从旧仓库名改成 `nv-action/vllm-benchmark`。 | `python3 tools/format_contributors.py [--repo ...]` | `--repo` 默认值更新。 |

---

## 5. 这批改动最值得关注的几个设计点

### 5.1 `dispatch_token` 是新的 stale guard 核心

旧架构里 stale 判断主要依赖本地状态和 run 轮询。新架构里，所有 outbound action 前都要刷新 `dispatch_token`，回调/终态 workflow 必须带着这个 token 回来。这样能避免：

- 老的 fix workflow 把新的状态覆盖掉
- bisect 慢回调误推进已经被新 head 替代的 PR
- terminal workflow 在 PR head 已变化后仍然创建 issue 或改状态

### 5.2 `waiting_bisect` 被提升为一等状态

这是很关键的设计变化。旧思路更像“phase3 内部顺带跑 bisect”。新设计明确承认 bisect 可能跑很久，因此：

- reconcile 可以看见 `waiting_bisect`
- bisect 结束后不直接 finalize，而是通过 reconcile 继续推进
- 如果 finalize 没跑成，reconcile 也能恢复

### 5.3 phase3 的 “无代码变更” 不再直接结束，而是显式进入 `manual_review_pending`

这一步不是简单的“失败”，而是：

1. 先把 state/register 评论推进到一个可观察的 pending 状态
2. 再 dispatch terminal workflow 去创建 issue

这样即使 terminal workflow 失败，PR 上也已经留下了准确的状态痕迹。

### 5.4 bisect runtime env 不再手工维护，而是从 CI workflow 读出来

这能避免下面的问题：

- bisect 跑的 runner/image 与正式 CI 不一致
- workflow 改了安装步骤，但 bisect helper 没同步
- 维护者要在两处更新同一份环境定义

---

## 6. 结论

从 `5c867bde...` 到当前 `HEAD` 的这批修改，本质上完成了：

- **控制面迁移**：从本地 daemon/MCP/service 迁到 GitHub Actions 原生状态机
- **状态存储迁移**：从本地 JSON 文件迁到 PR 结构化评论
- **工作流重构**：从单个长合同 workflow 拆成 detect/fix/reconcile/terminal/bisect 五个关注点清晰的单元
- **工具链升级**：`ci_log_summary.py`、`bisect_helper.py`、`bisect_vllm.sh` 变成新的标准入口
- **测试重建**：围绕新状态机、workflow 合同和 bisect 聚合逻辑重建测试

如果后续还需要，我可以继续在这份文档基础上补两类附录：

1. “旧状态机到新状态机”的逐状态转移图
2. “`main2main_ci.py` 每个子命令的输入/输出 JSON 示例”
