# 计划：310P nightly image build 测试（gy006 runner）+ pip 源切换 huaweicloud

- 分支：`test/310p-nightly-huaweicloud`（基于 `origin/main` @ `ee2e62fe2`）
- 工作区：`.worktrees/wt-310p-huaweicloud`
- 目标：测试 nightly **image build**（不是 nightly test），并验证 pip 源
  tuna → huaweicloud 的切换效果。
  背景产物：`swr.cn-north-12.myhuaweicloud.com/ascend/vllm-ascend:nightly-main-310p@sha256:3c7ef82d...`
  （既有 nightly-main-310p 产物，本次构建结果可与其对照）。

## 1. 改动清单

### 1.1 runner 切到 gy006（`_nightly_image_build.yaml`）

main 上 nightly build 用的是通用 `linux-*-cpu-4` 池（自动落 cn12-001）。
本次测试显式指定 gy006：

| 位置 | 改动 |
|---|---|
| L82 `generate-matrix` runs-on | `linux-amd64-cpu-4` → `linux-amd64-cpu-4-gy006` |
| L89 daily 矩阵 4 个 `arch_runner` | 全部加 `-gy006` 后缀 |
| L91 非daily 矩阵 `arch_runner` | 同上 |
| L265 merge job runs-on | 同上 |

### 1.2 pip 源切换（4 个 nightly Dockerfile，已改）

`Dockerfile.nightly.{a2,a3,310p,a5}` L24/26：

```dockerfile
# 原
ARG PIP_INDEX_URL="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"
# 改为
ARG PIP_INDEX_URL="https://repo.huaweicloud.com/repository/pypi/simple"
```

背景：squid 实测 tuna 回源仅 250~300KB/s（46.8MB pyarrow wheel 拉 153~234s），
且两 squid pod 缓存隔离导致重复回源；huaweicloud 是既有信任源，squid 有配套缓存规则。
`PIP_TRUSTED_HOST` 默认空、由 pip config 条件生效，无需改动。

## 2. 触发（只构建 310P）

`nightly_image_build.yaml` 的 `chips` 传 `["310p"]`（build-a2/a3/a5 四个 job
按 `contains(chips, X)` 过滤为 skipped，仅 build-310p 运行）：

```bash
gh workflow run nightly_image_build.yaml \
  --ref test/310p-nightly-huaweicloud \
  -f vllm_ascend_branch=main \
  -f build_type=daily \
  -f chips='["310p"]'
```

注意：
- main 上 nightly build 的 `should_push` 硬编码 false → 纯构建验证，
  不推 SWR、不污染现有 `nightly-main-310p` tag
- `build_type=daily` 时矩阵为 4 臂（310P × {ubuntu,openEuler} × {amd64,arm64}）
- **不要传 `skip_build_image`**——那是测试 workflow 的参数，与本次无关
- `schedule_nightly_test_310p.yaml` 已还原，保持零改动

## 3. 执行步骤

1. [x] `_nightly_image_build.yaml`：runner 加 `-gy006`（§1.1）
2. [x] 4 个 `Dockerfile.nightly.*`：PIP_INDEX_URL → huaweicloud（§1.2）
3. [ ] commit（`git commit -s`）+ push `test/310p-nightly-huaweicloud`
4. [ ] 触发 nightly build（§2 命令，chips=["310p"]）
5. [ ] 确认 build job 落在 gy006 runner

## 4. 验证清单

- [ ] build-310p 的 4 个 build job（ubuntu/openEuler × amd64/arm64）全部成功，均落在 `cpu-4-gy006`
- [ ] 构建日志 pip install 走 `repo.huaweicloud.com`（不再出现 tuna 慢拉；
      可对比 wheel 下载耗时，squid 侧应有 huaweicloud 的 TCP_HIT）
- [ ] `merge-image` 等推送类 job 因 should_push=false 正常 skipped
- [ ] 现有 `ascend/vllm-ascend:nightly-main-310p` 产物未被覆盖
