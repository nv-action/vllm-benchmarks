# BoostKit Performance Evaluation

本项目通过 GitHub Actions 在专用 Runner 上运行开源软件的 x86_64 与 aarch64 性能测试，生成单架构结果和跨架构报告。软件注册在 `config/categories.yaml`，各软件的版本、测试入口和指标由其 `case.yaml` 声明。

## 运行测试

在仓库的 Actions 页面手动运行对应工作流：

| 工作流 | 用途 | 当前默认输入 | 结果去向 |
|---|---|---|---|
| [Manual performance evaluation](.github/workflows/performance-test.yml) | 正式测试 | `sonic-go`、`1.15.2`、`all` 架构 | 完整结果上传华为 OBS；精简历史写入 `performance-results` 分支 |
| [Development performance evaluation](.github/workflows/development-performance-test.yml) | 开发验证 | `kubernetes`、`1.37.0`、`all` 架构 | 结果与汇总报告保存为 Actions Artifact，保留 30 天；不写正式历史或基线 |

`software` 和 `version` 可填写单项、逗号分隔的多项或 `all`；`architecture` 可选 `x86_64`、`aarch64` 或 `all`。若要运行全部已注册软件和版本，需显式将 `software`、`version` 都设为 `all`。正式工作流的 `update_baseline` 默认关闭；启用时必须运行双架构，且两个架构及清理均成功后才会更新基线。

两个工作流使用相同的测试阶段：

```text
校验清单并生成矩阵 → prepare → build → start → test → stop → finalize → cleanup → 汇总报告
```

每个软件、版本、架构是独立任务。`stop` 和 `cleanup` 在失败路径仍会执行。正式与开发工作流分别使用独立的 Runner 标签和并发组。

## Runner 要求

Runner 标签统一在 [config/defaults.yaml](config/defaults.yaml) 中维护：

| 用途 | x86_64 | aarch64 |
|---|---|---|
| 正式测试 | `PERF_RUNNER_X86_64` | `PERF_RUNNER_ARM64` |
| 开发验证 | `PERF_DEV_X86_64` | `PERF_DEV_ARM64` |

Runner 应是与标签架构一致的专用裸机，安装 Git、Python 3.11+、pip 和 `dnf`，并允许测试账户管理 `/home/runner/boostkit-perf` 及其测试进程。软件脚本通过 `dnf` 安装缺失的系统依赖；若需要代理，工作流从 `secrets.PERF_PROXY` 提供代理地址，脚本将其传给 `dnf`，不依赖 `sudo` 继承代理环境变量。

测试账户还需要免密执行 `dnf`，以及读取完整 CPU 型号所用的 `lscpu`。以下示例中的 `runner` 应替换为实际 Actions 服务账户；如软件另需特权操作，应单独配置：

```text
runner ALL=(root) NOPASSWD: /usr/bin/dnf, /usr/bin/env LC_ALL=C /usr/bin/lscpu
```

正式工作流上传和汇总结果还需要配置 `OBS_ENDPOINT`、`OBS_BUCKET`、`OBS_PREFIX`、`OBS_AK`、`OBS_SK`；使用临时凭据时配置 `OBS_SECURITY_TOKEN`。这些值通过 GitHub Actions Secrets 提供。

任务的源码、构建、安装、缓存和测试数据统一放在 `/home/runner/boostkit-perf/<软件名>/` 下。工作流仅在专用 Runner 上执行全局清理；清理会删除该工作根目录中的任务数据，请勿将其他资料放入其中。

## 结果与报告

单个任务在仓库工作区的结果目录为：

```text
.perf-output/<分类>/<软件>/<版本>/<架构>/<run_id>/
```

其中包含原始测试输出、阶段日志、`normalized_result.json` 和单架构 `report.md`。汇总阶段按同软件、同版本配对两个架构的结果，校验可比性并生成跨架构报告；缺失或不匹配的指标不会被强行对齐。正式测试的完整任务目录和汇总报告上传 OBS，供汇总阶段读取；`performance-results` 分支保存精简的不可变历史及可选基线。开发测试只使用 Actions Artifact，不写入 OBS 或正式历史。

## 本地校验与软件接入

在仓库根目录安装校验依赖并运行：

```bash
python3 -m pip install 'PyYAML==6.0.2' 'pytest>=8.0,<10.0'
python3 framework/catalog.py validate
python3 framework/catalog.py matrix --software all --version all --architecture all --pretty
python3 -m pytest framework/tests
```

新增或调整软件时，先阅读 [软件测试接入指南](doc/SOFTWARE_INTEGRATION.md)，再修改注册表、软件的 `case.yaml` 和测试脚本。该指南包含阶段接口、指标约定、离线包与网络下载策略，以及本地验收方法。
