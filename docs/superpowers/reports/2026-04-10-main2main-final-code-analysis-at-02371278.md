# Main2Main 最终代码分析（02371278）

## 0. 分析目标与范围

这份文档不再按 commit 差异展开，而是直接分析 `023712788d9eea7addf791adcff21d876e858598` 这个时点上 **main2main 子系统最终代码** 到底在做什么。

用户特别说明“要包含 `5c867bde59959198e8177b48a1ae519885ffbd4e` 这个修改本身”。因此这里的理解是：

- 文件范围以 `5c867bde...` 及其后续提交引入/调整过的 **main2main 相关文件** 为线索
- 解释对象则是这些文件在 `02371278` 时点上的 **最终形态**
- 不分析 commit 之间的演化细节
- 不逐条比较中间版本

### 本文聚焦的对象

本文只分析 **main2main 控制面及其直接依赖**，包括：

- 4 个主 workflow
- 2 个核心 workflow 脚本
- 2 个 bisect 工具
- 相关技能文档
- 相关设计/计划文档
- 相关测试文件
- 少量被 `bisect_helper.py` 直接读取的支撑 workflow

### 本文不展开的对象

以下文件虽然也在提交窗口内改过，但不是 main2main 的核心控制逻辑，因此只在必要处顺带提及，不单独做详细说明：

- `.github/workflows/_pre_commit.yml`
- `.github/workflows/bot_merge_conflict.yaml`
- `.github/workflows/labled_doctest.yaml`
- `.github/workflows/labled_download_model.yaml`
- `benchmarks/README.md`
- `tools/format_contributors.py`

---

## 1. main2main 最终代码整体做了什么

最终代码里的 `main2main` 是一套 **workflow-native 的自动适配控制面**。它的目标是：

1. 监控 upstream vLLM `main` 的提交变化
2. 自动生成一个 `main2main` PR
3. 等待该 PR 的 E2E-Full 结果
4. 根据 CI 结果决定下一步：
   - 直接把 PR 设为 ready
   - 进入 Phase 2 自动修复
   - 进入 Phase 3 的 bisect + 修复
   - 创建 manual review issue
5. 把全部在线状态保存在 PR 评论里，而不是本地服务状态文件里

一句话概括：

> 这套最终代码已经不依赖本地常驻 orchestrator/service，而是完全由 GitHub Actions + PR 评论状态 + 少量 Python helper 脚本来驱动。

### 最终架构的核心特点

| 维度 | 最终代码中的做法 |
|---|---|
| 控制面 | GitHub Actions workflow |
| 状态存储 | PR 结构化评论 |
| 状态守护 | `dispatch_token` |
| E2E 等待 | `schedule_main2main_reconcile.yaml` 定期 reconcile |
| Phase 2 修复 | `schedule_main2main_auto.yaml` 的 `fix_phase2` |
| Phase 3 修复 | `fix_phase3_prepare` -> bisect -> reconcile -> `fix_phase3_finalize` |
| 终态动作 | `dispatch_main2main_terminal.yaml` |
| CI 日志分析 | `.github/workflows/scripts/ci_log_summary.py` |
| bisect 环境生成 | `tools/bisect_helper.py` |
| 真正执行 bisect | `tools/bisect_vllm.sh` |

### 最终状态机主线

```text
detect
  -> 创建 PR
  -> 写入 main2main-register + main2main-state
  -> status=waiting_e2e

reconcile(waiting_e2e)
  -> E2E success  -> dispatch terminal make_ready
  -> E2E failure, phase=2 -> dispatch fix_phase2
  -> E2E failure, phase=3 -> dispatch fix_phase3_prepare
  -> merge conflict / terminal condition -> dispatch manual_review

fix_phase2
  -> 有代码变更 -> 推送分支 -> phase=3,status=waiting_e2e
  -> 无代码变更 -> phase=3,status=waiting_e2e

fix_phase3_prepare
  -> 生成 bisect payload
  -> 调度 bisect workflow
  -> status=waiting_bisect

reconcile(waiting_bisect)
  -> bisect 结束 -> dispatch fix_phase3_finalize

fix_phase3_finalize
  -> 有代码变更 -> phase=done,status=waiting_e2e
  -> 无代码变更 -> status=manual_review_pending -> dispatch terminal manual_review

terminal
  -> make_ready -> phase=done,status=ready
  -> manual_review -> phase=done,status=manual_review
```

---

## 2. 在线状态模型

最终代码里，在线状态由两个 PR 评论承载：

| 评论标记 | 作用 |
|---|---|
| `main2main-register` | 启动时的静态上下文，记录 PR、branch、head、commit range、phase |
| `main2main-state:v1` | 唯一在线状态源，记录当前 phase/status/run_id/token/terminal_reason 等 |

### `main2main-state` 字段

| 字段 | 作用 |
|---|---|
| `pr_number` | 目标 PR 编号 |
| `branch` | PR head branch |
| `head_sha` | 当前 PR head SHA |
| `old_commit` | 旧的 pinned vLLM commit |
| `new_commit` | 新的 upstream vLLM commit |
| `phase` | 阶段，通常是 `2`、`3`、`done` |
| `status` | 当前运行状态，如 `waiting_e2e`、`fixing`、`waiting_bisect`、`ready`、`manual_review` |
| `dispatch_token` | 当前动作 token，用于 stale guard |
| `e2e_run_id` | 当前关联的 E2E workflow run id |
| `fix_run_id` | 当前修复 workflow run id |
| `bisect_run_id` | 当前 bisect workflow run id |
| `terminal_reason` | 进入终态的原因，如 `workflow_error`、`phase3_no_changes` |
| `workflow_error_count` | workflow 重试次数 |
| `last_transition` | 最近一次状态转移 |
| `updated_at` | 最近更新时间 |
| `updated_by` | 最近更新来源，通常是 `workflow/job` 名称 |

### `dispatch_token` 的意义

`dispatch_token` 是最终代码里最重要的防串扰机制。

它的作用是防止以下问题：

- 老的 workflow run 回来覆盖新的状态
- 老的 bisect 回调推进已经不是当前 head 的 PR
- terminal workflow 基于过期上下文去改状态或建 issue

用法原则：

1. 每次要 dispatch 下一个动作前，先 mint 新 token
2. 把 token 写入 `main2main-state`
3. 被调 workflow 启动时必须校验传入 token 与当前 state token 一致
4. 不一致就视为 stale，直接 no-op

---

## 3. 核心文件总览

### 3.1 工作流文件

| 文件 | 类型 | 最终职责 |
|---|---|---|
| `.github/workflows/schedule_main2main_auto.yaml` | 主 workflow | detect、phase2 修复、phase3 prepare、phase3 finalize |
| `.github/workflows/schedule_main2main_reconcile.yaml` | 调度/协调 workflow | 轮询 open main2main PR，推进 `waiting_e2e` / `waiting_bisect` |
| `.github/workflows/dispatch_main2main_terminal.yaml` | 终态 workflow | `make_ready`、`manual_review` |
| `.github/workflows/dispatch_main2main_bisect.yaml` | bisect workflow | 运行 bisect，产出 summary/json，并通过 reconcile 回流主状态机 |

### 3.2 共享脚本与工具

| 文件 | 类型 | 最终职责 |
|---|---|---|
| `.github/workflows/scripts/main2main_ci.py` | 状态机 helper | 评论解析/渲染、状态转移、guard、reconcile、payload 生成 |
| `.github/workflows/scripts/ci_log_summary.py` | 日志分析脚本 | 从本地 log 或 GitHub run 提取 failed cases、root-cause、good/bad commit、bisect payload |
| `tools/bisect_helper.py` | bisect 辅助工具 | workflow 环境解析、matrix 生成、bisect 结果聚合 |
| `tools/bisect_vllm.sh` | bisect 执行脚本 | 真正执行 `git bisect`，输出 Markdown 报告 |

### 3.3 技能与说明文档

| 文件 | 类型 | 最终职责 |
|---|---|---|
| `.agents/skills/main2main-error-analysis/SKILL.md` | agent 技能 | 旧版单独错误分析技能 |
| `.agents/skills/main2main/SKILL.md` | agent 技能 | 较早的通用 main2main 适配技能 |
| `.agents/skills/main2main_v2/*` | agent 技能文档集 | 新版 main2main 技能说明 |
| `.claude/skills/main2main/*` | Claude 技能文档集 | workflow 中 Claude Code Action 实际使用的技能说明 |
| `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md` | 设计文档 | 最终架构设计说明 |
| `docs/superpowers/plans/2026-04-07-main2main-workflow-native-orchestration.md` | 实施计划 | 从旧控制面迁移到新控制面的实施步骤 |

### 3.4 测试文件

| 文件 | 最终职责 |
|---|---|
| `tests/main2main/test_main2main_ci.py` | 测 `main2main_ci.py` 的状态机和 helper 逻辑 |
| `tests/main2main/test_main2main_workflow_contract.py` | 测 workflow 合同和 shell 片段 |
| `tests/main2main/test_extract_and_analyze_contract.py` | 测 `ci_log_summary.py` 和 `bisect_helper.py` 的分析/聚合契约 |
| `tests/main2main/test_bisect_helper_runtime_env.py` | 测 bisect runtime env 是否准确读取 CI workflow |

---

## 4. 工作流文件逐个说明

### 4.1 [schedule_main2main_auto.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/schedule_main2main_auto.yaml)

**文件在做什么**

这是最终 main2main 的主 workflow，承担四种模式：

- `detect`
- `fix_phase2`
- `fix_phase3_prepare`
- `fix_phase3_finalize`

它既是入口，也是实际修复动作的执行者。

**怎么使用**

1. 自动定时触发：

```bash
# GitHub schedule
0 14 * * *
```

2. 手工 dispatch：

```bash
gh workflow run schedule_main2main_auto.yaml \
  --repo nv-action/vllm-benchmarks \
  -f mode=fix_phase2 \
  -f pr_number=188 \
  -f dispatch_token=<token>
```

**workflow_dispatch 参数**

| 参数 | 作用 |
|---|---|
| `mode` | 执行模式。允许 `detect`、`fix_phase2`、`fix_phase3_prepare`、`fix_phase3_finalize` |
| `target_commit` | 仅 detect 模式使用。为空时取 upstream `main` HEAD；有值时必须是 40 位 SHA |
| `pr_number` | fix 模式针对的 PR 编号 |
| `dispatch_token` | 当前受保护 token，fix 模式必须传入并通过 guard 检查 |
| `bisect_run_id` | `fix_phase3_finalize` 可传的 bisect run id；为空时脚本可从 state 中读取 |

**内部 job**

| Job | 作用 |
|---|---|
| `detect-and-adapt` | 检测 upstream vLLM 是否变化，调用 Claude 适配，建 draft PR，写入 register/state 评论 |
| `fix-phase2` | 根据 E2E 失败做自动修复，并把 phase 推到 `3` |
| `fix-phase3-prepare` | 调用 `ci_log_summary.py` + `main2main_ci.py` 生成 bisect payload，dispatch bisect workflow |
| `fix-phase3-finalize` | 读取 bisect 结果做 phase3 修复，决定回到 `waiting_e2e` 还是进入 manual review |

**关键环境变量**

| 变量 | 作用 |
|---|---|
| `UPSTREAM_REPO` | 控制仓库，一般是 `nv-action/vllm-benchmarks` |
| `FORK_OWNER` | 工作分支所在 fork owner |
| `CONTROL_REPO_DIR` | control repo checkout 目录 |
| `WORK_REPO_DIR` | 可写工作副本 checkout 目录 |
| `MAIN2MAIN_MODEL` | Claude 模型名 |
| `GH_TOKEN` | `gh` 使用的 PAT |
| `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` | Claude gateway 配置 |

**最终代码里的实际意义**

这个文件是“主执行器”，但它不负责长期等待。等待逻辑交给 reconcile。

---

### 4.2 [schedule_main2main_reconcile.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/schedule_main2main_reconcile.yaml)

**文件在做什么**

这是最终控制面的“调度中枢”。

它定期扫描 open 的 main2main PR，负责：

- 初始化缺失的 `main2main-state`
- 解析当前 PR 对应的 E2E 状态
- 识别 `waiting_bisect` 是否已经满足 finalize 条件
- 根据 `phase + status + mergeability` 决定下一步 action

**怎么使用**

1. 自动 schedule：

```bash
*/10 * * * *
```

2. 手工只 reconcile 某个 PR：

```bash
gh workflow run schedule_main2main_reconcile.yaml \
  --repo nv-action/vllm-benchmarks \
  -f pr_number=188
```

**参数**

| 参数 | 作用 |
|---|---|
| `pr_number` | 可选。为空时扫描所有 open main2main PR；有值时只处理指定 PR |

**最终代码里的实际意义**

这个文件替代了旧的本地 `poll_loop` / `run_once` 常驻服务。

---

### 4.3 [dispatch_main2main_terminal.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/dispatch_main2main_terminal.yaml)

**文件在做什么**

处理 main2main 的终态动作：

- `make_ready`
- `manual_review`

**怎么使用**

```bash
gh workflow run dispatch_main2main_terminal.yaml \
  --repo nv-action/vllm-benchmarks \
  -f action=manual_review \
  -f pr_number=188 \
  -f dispatch_token=<token> \
  -f terminal_reason=phase3_no_changes
```

**参数**

| 参数 | 作用 |
|---|---|
| `action` | 终态动作，允许 `make_ready` 或 `manual_review` |
| `pr_number` | 目标 PR 编号 |
| `dispatch_token` | 当前 state token，必须匹配 |
| `terminal_reason` | manual review 原因，如 `phase3_no_changes`、`workflow_error` |

**内部步骤**

| 步骤 | 作用 |
|---|---|
| `load-phase-context` | 从 PR、评论、state 中装配上下文 |
| `gh pr ready` | `make_ready` 时把 draft PR 转为 ready |
| `ci_log_summary.py` | `manual_review` 时提取失败分析上下文 |
| Claude issue body 生成 | 把上下文整理成 markdown issue |
| `gh issue create` | 创建 manual review issue |
| `state-write` | 把状态推进到 `ready` 或 `manual_review` |

**最终代码里的实际意义**

它是旧 `terminal_worker.py` 的 workflow 版替代物。

---

### 4.4 [dispatch_main2main_bisect.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/dispatch_main2main_bisect.yaml)

**文件在做什么**

这是最终 bisect workflow。支持两种调用模式：

- `standalone`
- `main2main`

**怎么使用**

```bash
gh workflow run dispatch_main2main_bisect.yaml \
  --repo nv-action/vllm-benchmarks \
  -f caller_type=main2main \
  -f caller_run_id=24000000000 \
  -f good_commit=<good> \
  -f bad_commit=<bad> \
  -f test_cmd='pytest -sv tests/e2e/singlecard/test_x.py::test_y' \
  -f main2main_pr_number=188 \
  -f main2main_dispatch_token=<token>
```

**参数**

| 参数 | 作用 |
|---|---|
| `caller_type` | `standalone` 或 `main2main` |
| `caller_run_id` | 上游 workflow run id，用于追踪 |
| `good_commit` | 已知好提交 |
| `bad_commit` | 已知坏提交 |
| `test_cmd` | 失败测试命令，支持分号分隔多个命令 |
| `main2main_pr_number` | 仅 main2main 模式需要，用于回调上下文 |
| `main2main_dispatch_token` | 仅 main2main 模式需要，用于 stale guard 追踪 |

**内部 job**

| Job | 作用 |
|---|---|
| `set-params` | 调用 `tools/bisect_helper.py batch-matrix` 生成矩阵 |
| `bisect` | 按组执行 bisect |
| `upload-all-results` | 合并各组 summary，生成 `bisect_result.json` |
| `callback-main2main` | 若是 main2main 模式，不直接 finalize，而是触发 reconcile |

**最终代码里的实际意义**

它的关键变化是：**bisect 完成后不直接推进主状态机，而是回到 reconcile 统一决策。**

---

## 5. 脚本与工具逐个说明

### 5.1 [main2main_ci.py](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/scripts/main2main_ci.py)

**文件在做什么**

这是整个最终架构的核心 helper 脚本。它把原本散在本地 orchestrator 里的逻辑抽出来，变成一个可以在 workflow 里被命令行调用的统一入口。

它负责：

- 解析/渲染 `main2main-register` 评论
- 解析/渲染 `main2main-state:v1` 评论
- token mint
- stale guard
- PR/state/register 一致性校验
- `waiting_e2e` / `waiting_bisect` 的 reconcile 决策
- bisect payload 生成
- workflow 失败后的恢复动作计算

**怎么使用**

顶层帮助：

```bash
python3 .github/workflows/scripts/main2main_ci.py -h
```

**子命令总览**

| 子命令 | 作用 | 关键参数 |
|---|---|---|
| `mint-dispatch-token` | 生成新 token | 无 |
| `state-read` | 从评论文本解析 state JSON | `--comment-file` |
| `state-write` | 把 state JSON 渲染成评论文本 | `--json-file` |
| `registration-read` | 解析 register 评论 | `--comment-file` |
| `registration-write` | 渲染 register 评论 | `--json-file` |
| `state-init-from-register` | 从 register 初始化 state | `--comment-file --dispatch-token` |
| `guard-check` | 校验 phase/status/token | `--state-file [--expected-phase] [--expected-status] [--dispatch-token]` |
| `pr-consistency-check` | 校验 branch/head 是否一致 | `--state-file --branch --head-sha` |
| `registration-consistency-check` | 校验 register 与 state 的 commit/branch/phase 是否一致 | `--state-file --registration-file` |
| `reconcile-decision` | 纯函数方式计算下一步动作 | `--state-file [--e2e-run-file] [--merge-state-status] [--mergeable]` |
| `reconcile-pr` | 对一个 PR 执行完整 reconcile | `--repo --pr-number` |
| `select-e2e-run` | 从 run 列表选中与当前 head 匹配的 E2E run | `--runs-file --head-sha` |
| `apply-fix-result` | 根据 fix 结果推进 phase | `--state-file --result --new-head-sha` |
| `json-get` | 从 JSON 文件读取字段 | `--json-file --field` |
| `upsert-pr-phase-section` | 更新 PR body 某个 phase 小节 | `--body-file --heading --content-file --output-file` |
| `prepare-detect-artifacts` | 生成 detect 阶段的初始 state/register/comment 文件 | 多个 `--*-out` 参数 |
| `prepare-fix-transition` | 生成 fix 后的新 state/register/comment | `--state-file --result --fix-run-id --last-transition --updated-by ...` |
| `prepare-bisect-payload` | 用 CI 分析结果生成 bisect 输入 | `--state-file --ci-analysis-file [--payload-json-out]` |
| `prepare-fixing-state` | 生成 `status=fixing` 的 state/comment | `--state-file --fix-run-id --last-transition --updated-by ...` |
| `prepare-waiting-bisect` | 生成 `status=waiting_bisect` 的 state/comment | `--state-file --bisect-run-id --fix-run-id --last-transition --updated-by ...` |
| `prepare-manual-review-pending` | 生成 pending terminal 状态 | `--state-file --terminal-reason --fix-run-id --last-transition --updated-by ...` |
| `prepare-workflow-error-action` | 计算当前失败 workflow 的下一步是 retry 还是 terminal | `--state-file --next-dispatch-token --max-retries ...` |
| `prepare-workflow-error-recovery` | 恢复版 error action，内部重新 mint token | `--state-file --max-retries ...` |
| `extract-pr-comments` | 从 comment 列表中抽出 state/register 评论和 comment id | `--comments-file ...` |
| `select-bisect-run-id` | 通过 `caller_run_id + dispatch_token` 找 bisect run id | `--runs-file --caller-run-id --dispatch-token` |
| `load-phase-context` | 一次性加载 PR + state + register + comment ids + context | `--repo --pr-number ...` |

**最常用的参数解释**

| 参数 | 作用 |
|---|---|
| `--state-file` | 指向 state JSON 文件 |
| `--registration-file` | 指向 register JSON 文件 |
| `--comment-file` | 指向评论 markdown 文件 |
| `--dispatch-token` | 当前 token |
| `--expected-phase` | guard 要求的 phase |
| `--expected-status` | guard 要求的 status |
| `--allowed-statuses` | `load-phase-context` 可接受的一组状态 |
| `--updated-by` | 写回 state 时记录是谁更新的 |
| `--last-transition` | 写回 state 时记录最近转移名称 |
| `--*-out` | 输出文件路径 |

**最终代码里的实际意义**

如果说 workflow 文件是“调度器”，那 `main2main_ci.py` 就是“状态机内核”。

---

### 5.2 [ci_log_summary.py](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/scripts/ci_log_summary.py)

**文件在做什么**

统一的 CI 日志分析脚本。它既可以分析本地 pytest log，也可以分析 GitHub Actions run。

它会提取：

- failed test files
- failed test cases
- root-cause exception
- code bugs / env flakes
- good commit / bad commit
- bisect payload 所需的 representative test cases

**怎么使用**

```bash
python3 .github/workflows/scripts/ci_log_summary.py \
  --repo nv-action/vllm-benchmarks \
  --run-id 23127187822 \
  --format llm-json \
  --output /tmp/ci_analysis.json
```

或分析本地日志：

```bash
python3 .github/workflows/scripts/ci_log_summary.py \
  --log-file /tmp/unit-test.log \
  --mode ut \
  --step-name "Run unit test"
```

**参数**

| 参数 | 作用 |
|---|---|
| `--log-file` | 本地日志文件，与 `--run-id` 二选一 |
| `--run-id` | GitHub Actions run id |
| `--repo` | `--run-id` 模式使用的 repo |
| `--mode` | `ut` 或 `e2e`，影响 summary 呈现 |
| `--step-name` | 输出摘要时显示的步骤名 |
| `--format` | `summary`、`json`、`llm-json`、`bisect-json` |
| `--output` | 输出路径，不给则 stdout |

**格式含义**

| 格式 | 用途 |
|---|---|
| `summary` | 人看的 markdown 摘要 |
| `json` | 完整结构化结果 |
| `llm-json` | 给技能/Claude 用的压缩 JSON |
| `bisect-json` | 给 phase3 bisect payload 用的 JSON |

---

### 5.3 [bisect_helper.py](/Users/antarctica/Work/PR/vllm-benchmarks/tools/bisect_helper.py)

**文件在做什么**

这个脚本是最终 bisect 体系的“环境翻译器”和“结果聚合器”。

它的关键设计点是：

> 不再自己硬编码 runner/image/install env，而是直接读取仓库里的 `_e2e_test.yaml` 和 `_unit_test.yaml` 作为事实来源。

**怎么使用**

```bash
python3 tools/bisect_helper.py -h
```

**子命令**

| 子命令 | 作用 | 参数 |
|---|---|---|
| `batch-matrix` | 把分号分隔测试命令转换为 GitHub Actions matrix | `--test-cmds --output-format` |
| `get-commit` | 从 workflow yaml 提取 vLLM commit | `--yaml-path [--ref]` |
| `report` | 生成 bisect Markdown 报告 | `--good-commit --bad-commit --first-bad --test-cmd --total-steps --total-commits ...` |
| `vllm-location` | `pip show` 获取 vllm 位置 | 无 |
| `vllm-install` | 针对某个测试命令生成 vllm 安装命令 | `--test-cmd` |
| `ascend-install` | 针对某个测试命令生成仓库安装命令 | `--test-cmd` |
| `result-json` | 聚合多组 bisect summary，输出机器可读 JSON | `--caller-type --caller-run-id --bisect-run-id --good-commit --bad-commit --test-cmd --results-dir ...` |

**重要参数**

| 参数 | 作用 |
|---|---|
| `--test-cmds` | 多个测试命令，分号分隔 |
| `--output-format` | `json` 或 `github` |
| `--yaml-path` | 读取 commit 的 workflow 文件路径 |
| `--ref` | 从某个 git ref 读取 workflow 文件 |
| `--results-dir` | 搜集各组 bisect result 的目录 |
| `--caller-type` | `standalone` / `main2main` |
| `--caller-run-id` | 上游调用 run id |
| `--bisect-run-id` | 当前 bisect workflow run id |

---

### 5.4 [bisect_vllm.sh](/Users/antarctica/Work/PR/vllm-benchmarks/tools/bisect_vllm.sh)

**文件在做什么**

真正执行 `git bisect` 的 shell 工具。

它负责：

- 检测 good/bad commit
- 检测 vllm repo / 仓库 repo 路径
- 保证 vllm repo 是干净工作树
- 在 bisect 过程中按需重装 vllm 与仓库
- 运行测试并把结果转成 `good/bad/skip`
- 生成 Markdown bisect 报告

**怎么使用**

```bash
bash tools/bisect_vllm.sh --help
```

示例：

```bash
./tools/bisect_vllm.sh \
  --good <good_sha> \
  --bad <bad_sha> \
  --test-cmd "pytest -sv tests/ut/test_example.py::test_case"
```

**参数**

| 参数 | 作用 |
|---|---|
| `--test-cmd` | 单个失败测试命令 |
| `--test-cmds-file` | 批量模式使用的命令文件 |
| `--good` | 已知好提交 |
| `--bad` | 已知坏提交 |
| `--vllm-repo` | vllm 仓库路径 |
| `--ascend-repo` | 当前仓库路径 |
| `--env` | 额外环境变量 |
| `--fetch-depth` | bisect 前 fetch 深度，`0` 表示尽量完整历史 |
| `--step-timeout` | 单步测试超时 |
| `--total-timeout` | 总 bisect 超时 |
| `--summary-output` | summary 输出路径 |

---

## 6. 技能与文档文件逐个说明

### 6.1 agent 侧技能文件

| 文件 | 文件在做什么 | 使用方法 | 参数/输入 |
|---|---|---|---|
| `.agents/skills/main2main-error-analysis/SKILL.md` | 较早的“单独错误分析技能”，指导如何先跑 `ci_log_summary.py`，再做 root cause 分析与修复 | 给 agent 阅读执行 | 主要输入是 `RUN_ID`、本地 vLLM 仓库、`GOOD_COMMIT`、`BAD_COMMIT` |
| `.agents/skills/main2main/SKILL.md` | 较早的通用 main2main 技能，偏“主动升级 + 生成 `vllm_changes.md`” | 给 agent 阅读执行 | 主要输入是当前 pinned commit 与 upstream 最新 commit |
| `.agents/skills/main2main_v2/SKILL.md` | 新版主入口，负责场景识别：主动升级还是 CI 故障诊断 | 给 agent 技能入口使用 | 无 CLI 参数；由用户意图决定读取哪个子文档 |
| `.agents/skills/main2main_v2/error-analysis.md` | v2 的 CI 故障诊断主文档 | 被 `main2main_v2/SKILL.md` 指向 | 输入为 run id、good/bad commit、本地 vLLM 仓库 |
| `.agents/skills/main2main_v2/proactive-upgrade.md` | v2 的主动升级主文档 | 被 `main2main_v2/SKILL.md` 指向 | 输入为 `old_commit`、`new_commit` |
| `.agents/skills/main2main_v2/reference/error-patterns.md` | 常见错误模式速查表 | 在错误分析阶段被引用 | 输入是具体错误类型/报错模式 |
| `.agents/skills/main2main_v2/evals/evals.json` | 技能评估占位文件 | 供未来 eval 使用 | 无 |

### 6.2 Claude 侧技能文件

| 文件 | 文件在做什么 | 使用方法 | 参数/输入 |
|---|---|---|---|
| `.claude/skills/main2main/SKILL.md` | Claude 侧主技能入口。detect/fix workflow 里的 prompt 都是“Use the main2main skill”，实际会落到这里 | Claude Code Action 读取 | 无 CLI 参数 |
| `.claude/skills/main2main/error-analysis.md` | Claude 侧 CI 故障诊断说明 | 供 phase2/phase3 修复 prompt 使用 | 典型输入是 `RUN_ID`、分析 JSON、本地 vLLM 仓库 |
| `.claude/skills/main2main/proactive-upgrade.md` | Claude 侧主动升级说明 | 供 detect/adapt prompt 使用 | 输入为 `OLD_COMMIT`、`NEW_COMMIT`、本地 upstream checkout |
| `.claude/skills/main2main/reference/error-patterns.md` | Claude 侧错误模式参考 | 在错误诊断 prompt 中配合使用 | 输入是错误类型，不是 CLI 参数 |

### 6.3 设计与实施文档

| 文件 | 文件在做什么 | 使用方法 | 参数/输入 |
|---|---|---|---|
| `docs/superpowers/specs/2026-04-07-main2main-workflow-native-orchestration-design.md` | 最终主设计文档，定义状态模型、workflow 合同和迁移目标 | 设计参考 | 无 |
| `docs/superpowers/plans/2026-04-07-main2main-workflow-native-orchestration.md` | 迁移实施计划 | 实施参考 | 无 |
| `docs/superpowers/specs/2026-03-12-main2main-mcp-service-design.md` | 历史设计文档，记录旧 MCP/service 方案 | 历史背景参考 | 无 |
| `docs/superpowers/plans/2026-03-12-main2main-mcp-service.md` | 历史实施计划，记录旧常驻服务方案 | 历史背景参考 | 无 |

---

## 7. 测试文件逐个说明

| 文件 | 文件在做什么 | 使用方法 | 参数/输入 |
|---|---|---|---|
| `tests/main2main/test_main2main_ci.py` | 覆盖 `main2main_ci.py` 的评论 round-trip、guard、reconcile 决策、phase 转移、error recovery、payload 生成、bootstrap 恢复 | `pytest -q tests/main2main/test_main2main_ci.py` | 无额外参数 |
| `tests/main2main/test_main2main_workflow_contract.py` | 验证 workflow 文件名、输入合同、关键 shell 语句、checkout 顺序、工作目录、bisect 回流策略 | `pytest -q tests/main2main/test_main2main_workflow_contract.py` | 无额外参数 |
| `tests/main2main/test_extract_and_analyze_contract.py` | 验证 `ci_log_summary.py` 的 repo override、`bisect-json` 输出，以及 `bisect_helper.py result-json` 聚合语义 | `pytest -q tests/main2main/test_extract_and_analyze_contract.py` | 无额外参数 |
| `tests/main2main/test_bisect_helper_runtime_env.py` | 验证 `bisect_helper.py` 是否正确从 `_unit_test.yaml` / `_e2e_test.yaml` 提取 runner、image、install/runtime env | `pytest -q tests/main2main/test_bisect_helper_runtime_env.py` | 无额外参数 |

---

## 8. 被直接依赖的支撑文件

### 8.1 [_e2e_test.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/_e2e_test.yaml)

**文件在做什么**

这是仓库 E2E 测试的标准 workflow 定义。

**为什么 main2main 要关心它**

`tools/bisect_helper.py` 会读取这个文件，提取：

- runner
- container image
- system dependencies
- vllm install step
- 仓库 install step
- runtime env

也就是说，它不仅是 CI 配置，还是 bisect 运行环境的“权威事实源”。

### 8.2 [_unit_test.yaml](/Users/antarctica/Work/PR/vllm-benchmarks/.github/workflows/_unit_test.yaml)

**文件在做什么**

标准 unit test workflow。

**为什么 main2main 要关心它**

`bisect_helper.py` 也会读取它，为 UT 场景构造 bisect 运行环境。

---

## 9. main2main 最终代码的实际使用方式

### 9.1 自动运行路径

1. `schedule_main2main_auto.yaml` 定时 detect
2. 若 upstream vLLM 漂移，自动建 draft PR
3. PR 带 `main2main` 相关 label，触发 E2E-Full
4. `schedule_main2main_reconcile.yaml` 定期检查该 PR
5. 若失败，推进 `fix_phase2` 或 `fix_phase3_prepare`
6. Phase 3 中触发 `dispatch_main2main_bisect.yaml`
7. bisect 完成后回到 reconcile
8. reconcile 再决定是否调 `fix_phase3_finalize`
9. 最终由 `dispatch_main2main_terminal.yaml` 把 PR 设为 ready 或创建 manual review issue

### 9.2 手工运维常用入口

| 目的 | 命令 |
|---|---|
| 重新 detect | `gh workflow run schedule_main2main_auto.yaml -f mode=detect` |
| 只 reconcile 某个 PR | `gh workflow run schedule_main2main_reconcile.yaml -f pr_number=<PR>` |
| 强制 terminal make_ready | `gh workflow run dispatch_main2main_terminal.yaml -f action=make_ready -f pr_number=<PR> -f dispatch_token=<TOKEN>` |
| 手工跑 bisect | `gh workflow run dispatch_main2main_bisect.yaml -f caller_type=standalone -f good_commit=<GOOD> -f bad_commit=<BAD> -f test_cmd='<CMD>'` |
| 本地看 state 逻辑 | `python3 .github/workflows/scripts/main2main_ci.py -h` |
| 本地看 bisect helper | `python3 tools/bisect_helper.py -h` |
| 本地看 bisect shell | `bash tools/bisect_vllm.sh --help` |

---

## 10. 结论

按最终代码来理解，`main2main` 已经是一套完整的 **GitHub Actions 原生自动适配控制面**：

- `schedule_main2main_auto.yaml` 负责创建和执行修复
- `schedule_main2main_reconcile.yaml` 负责等待和推进状态
- `dispatch_main2main_terminal.yaml` 负责终态动作
- `dispatch_main2main_bisect.yaml` 负责定位 first bad commit
- `main2main_ci.py` 是状态机内核
- `ci_log_summary.py` 是日志诊断入口
- `bisect_helper.py + bisect_vllm.sh` 是 bisect 工具链

这套最终代码最重要的设计变化不是“多了几个 workflow”，而是：

1. **状态从本地文件迁到 PR 评论**
2. **控制逻辑从本地常驻服务迁到 Actions**
3. **长期等待由 reconcile workflow 接管**
4. **通过 `dispatch_token` 实现严格的 stale guard**
5. **bisect 结果变成机器可读 JSON，而不是只留 Markdown summary**

如果还需要更细的版本，我下一步可以继续补两种附录之一：

1. `main2main_ci.py` 每个子命令的输入/输出 JSON 示例
2. 整个 main2main 状态机的时序图 / 流程图

