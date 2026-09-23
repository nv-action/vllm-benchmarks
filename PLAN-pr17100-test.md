# 计划：基于 nv-action 资源测试 vllm-ascend PR 17100（仅两个构建 workflow）

- 分支：`test/pr17100-nvaction`（基于 `origin/main` @ `ee2e62fe2` 创建）
- 工作区：`.worktrees/wt-pr17100-test`
- 目标：验证上游 [vllm-project/vllm-ascend#17100](https://github.com/vllm-project/vllm-ascend/pull/17100)（feat/remove-internal-cache）的构建代码，全流程走 nv-action 后端资源。
- **范围**：只涉及 `schedule_image_build_and_push.yaml`（image build）和
  `nightly_image_build.yaml`（nightly image build）两个**构建** workflow。
  **不改动任何 `schedule_nightly_test_*.yaml` 测试文件**（含 a3_560t，明确排除）。

## 0. 总体管线设计

现状问题：image-build 和 nightly 各建一遍镜像（nightly 测试 workflow 内置 `build-image` job，每次从源码重新构建 30-70 分钟）。

目标管线（构建一次、测试复用）：

```
┌─ image-build：schedule_image_build_and_push（一次全量 4 芯片 × 2 OS，should_push=true）
│     产物（daily 模式）：
│       atlas_inference/vllm-ascend:nightly-<branch>-<suffix>-cann<ver>-<date>   ← 最终多架构
│       atlas_inference/vllm-atlas-temp:nightly-<...>-<arch>-<date>-temp        ← 单架构临时
└─► nightly 测试：schedule_nightly_test_{a2,a3,a3_560t,310p,a5}
      触发时 skip_build_image=true（跳过内置 build-image job）
      测试 job 拉取 image-build 的产物镜像跑 NPU 测试
```

## 1. 后端资源：无需改 runner label

两套构建路径的 runner label 已是**通用 `cpu-4` 池**，自动调度到 cn12-001：

| 文件 | 位置 | 现状 |
|---|---|---|
| `_schedule_image_build.yaml` | L182-183（build matrix） | `linux-aarch64-cpu-4` / `linux-amd64-cpu-4` |
| `_schedule_image_build.yaml` | merge-image / merge-image-temp | `linux-amd64-cpu-4` |
| `_nightly_image_build.yaml` | L89-93（generate-matrix） | `linux-amd64-cpu-4` / `linux-aarch64-cpu-4` |

**动作：零改动。**

## 2. 指定 PR 17100 代码：用现有入参，无需改代码

- **image-build**（`schedule_image_build_and_push.yaml` → `_schedule_image_build.yaml`）：
  入参 `vllm_ascend_commit`（L58）传 PR 17100 的 head SHA
- **nightly 测试**：测试 job 本身不构建代码，只消费镜像，无需传 ref

> head SHA 获取：`gh api repos/vllm-project/vllm-ascend/pulls/17100 --jq .head.sha`

## 3. image-build 一次全量并推送产物（2 处改动）

**发现**：main 上 `image_build` job 硬编码 `should_push: false`——dispatch 只做 build check，
产物（`atlas_inference/vllm-ascend:nightly-<pattern><os>-cann<ver>-<date>`）根本不落 SWR，
nightly 自然引用不到。这是断链的根因。

**改动（`schedule_image_build_and_push.yaml`）**：

1. L158：`should_push: false` → `should_push: ${{ github.event_name != 'pull_request' }}`
   （PR 事件保持 build check 不推镜像；dispatch/schedule 推送产物）
2. `branch` choice 选项增加 `pull/17100/head`（choice 类型会校验，不加无法传参）

**触发**（走 `image_build` job，checkout vllm-ascend @ `pull/17100/head`，8 臂全量）：

```bash
gh workflow run schedule_image_build_and_push.yaml \
  --ref test/pr17100-nvaction \
  -f build_type=daily \
  -f branch=pull/17100/head -f tag=none
```

产物：`atlas_inference/vllm-ascend:nightly-pull17100head{,-openeuler}-cann<ver>-<date>`
（PATTERN 由 `pull/17100/head` 去斜杠得 `pull17100head`）

## 4. nightly image build：桥接已存在，零改动

**发现**：daily 模式下 `_nightly_image_build.yaml` 的 build 步骤**本就 FROM image-build 产物**：

```
BASE_IMAGE = atlas_inference/vllm-ascend:nightly-<branch><os_mark>-cann<ver>-<date>
（imagetools inspect 从今天回溯 30 天找最近产物；OS_MARK: ubuntu=""、openeuler="-openeuler"）
```

即 nightly 各芯片（a2/a3/310p/a5）都在 image-build 的 a2 系产物上叠加芯片层。
只要 §3 的产物落了 SWR，nightly 自动引用到 PR 17100 的代码产物——无需桥接代码。

**唯一注意**：`nightly_image_build.yaml` 的 `vllm_ascend_branch` 也是 choice，
已同样加入 `pull/17100/head` 选项。**nightly 必须等 image-build 完成后再触发**（同日用最新产物）：

```bash
gh workflow run nightly_image_build.yaml \
  --ref test/pr17100-nvaction \
  -f vllm_ascend_branch=pull/17100/head \
  -f build_type=daily
```

`should_push` 硬编码 false（纯构建验证 + 预热），nightly build 的 `BRANCH_TAG`
（`pull17100head`）与 image-build 的 PATTERN 一致，30 天回溯必命中当日产物。

> 说明：`schedule_nightly_test_*.yaml`（含 a3_560t）的 build-image 复用与镜像参数化
> 本期**不做**，测试 workflow 文件零改动。

## 5. 注释掉镜像推送的 quay sync（必须）

"sync" 指构建完成后 dispatch `ascend-gha-runners/sync-tools` 的 `sync-vllm-ascend.yml`
把 SWR 镜像镜像到 quay.io，共 **3 处**：

| 文件 | 位置 | 条件 |
|---|---|---|
| `_schedule_image_build.yaml` | `merge-image` 内约 L620 | `should_push == true` |
| `_schedule_image_build.yaml` | `merge-image-temp` 内约 L753 | `should_push == true` |
| `_nightly_image_build.yaml` | 约 L352 | 无条件 |

**动作：3 个 `Dispatch quay.io sync` 步骤整块物理注释**——§3 放开 should_push 后
merge job 会真实运行，不注释就必然 dispatch quay 同步。

## 6. 执行步骤

1. [x] `schedule_image_build_and_push.yaml`：should_push 放开（PR 除外）+ branch 选项加 `pull/17100/head`
2. [x] `nightly_image_build.yaml`：`vllm_ascend_branch` 选项加 `pull/17100/head`
3. [x] 注释 3 处 `Dispatch quay.io sync`（§5）
4. [ ] commit（`git commit -s`）并 push `test/pr17100-nvaction`
5. [ ] 触发 image-build 全量（§3 命令）
6. [ ] **等 image-build 完成**（8 build + merge 产物落 SWR）
7. [ ] 触发 nightly image build 全量（§4 命令）

## 7. 验证清单

- [ ] image-build：8 个 build job（4 芯片 × 2 OS）+ 2 个 merge job 全部成功，产物 `nightly-pull17100head{,-openeuler}-cann<ver>-<date>` 落 `atlas_inference/vllm-ascend`
- [ ] 构建日志确认 vllm-ascend 源为 `pull/17100/head`
- [ ] 无 `sync-tools` dispatch 调用（job 日志无 "Dispatch quay.io sync"）
- [ ] nightly build：4 个 job（a2/a3/310p/a5 × arm64/amd64/openEuler 矩阵）成功，日志显示 BASE_IMAGE 命中当日 `nightly-pull17100head...` 产物
- [ ] PR 事件（label image-build）仍为 build check，不推镜像
- [ ] `schedule_nightly_test_*.yaml` 等测试文件保持零改动
