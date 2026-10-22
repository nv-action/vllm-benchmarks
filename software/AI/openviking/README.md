# OpenViking 性能测试说明

本目录测试 OpenViking 官方 PyPI 预编译 wheel，并复用 openEuler 参考方案
的 AGFS（RAGFS）基准：通过 `openviking.pyagfs` 绑定客户端挂载内存型
`memfs` 文件系统，测量文件系统操作吞吐与延迟、客户端初始化延迟以及多
线程扩展性，进行 x86_64 与 aarch64 开箱性能对比。软件入口为
`openviking_test.sh`，Framework 通过 `case.yaml` 调用其 `build`、`start`、
`test`、`stop` 四个阶段；直接执行该脚本也使用同一套阶段函数。

当前清单仅声明 OpenViking `0.4.17.1`。新增版本时，必须同步更新
`case.yaml` 的版本与包索引声明，以及 `openviking_test.sh` 中的 Python
版本支持矩阵，不能跳过版本校验。

## 构建与安装

这里的“构建”是部署官方预编译 wheel，不从
`https://github.com/volcengine/OpenViking` 源码仓库编译，也不安装系统级
openviking RPM。

1. 确认系统 `python3` 版本在官方 wheel 支持矩阵内（`3.10`–`3.13`）。
2. 在本次任务工作目录 `PERF_WORK_DIR/openviking-venv` 中创建任务私有虚拟
   环境（框架会为每个 case 预建 `PERF_WORK_DIR/venv`，该路径被框架保留）。
3. 从华为云 PyPI 镜像安装精确版本（国产 Runner 无法稳定访问
   `files.pythonhosted.org`）：

```bash
python3 -m venv "${PERF_WORK_DIR}/openviking-venv"
"${PERF_WORK_DIR}/openviking-venv/bin/pip" install --no-cache-dir \
  --index-url https://mirrors.huaweicloud.com/repository/pypi/simple \
  --trusted-host mirrors.huaweicloud.com \
  "openviking==0.4.17.1"
```

4. 导入部署的 openviking 并校验 `openviking.__version__` 必须精确等于
   `0.4.17.1`，校验失败立即退出；实际版本写入
   `results/<版本>/<运行 ID>/actual-version.txt`。
5. 运行时缺失的系统依赖（`python3`、`python3-pip`、`curl` 等）由脚本
   自动通过 `dnf` 安装；非 root Runner 使用 `sudo -n dnf`。

虚拟环境仅供本次任务使用，不写入系统 Python 环境。

## 运行时启动

`start` 阶段对部署的 AGFS 绑定运行时执行冒烟循环：`get_binding_client()`
取得 RAGFS 绑定客户端，挂载 `memfs`，写入并回读固定负载，校验回读内容
与写入内容逐字节一致，最后卸载挂载点。冒烟通过后才进入测试阶段；不会
复用预先存在的系统级 openviking 安装。

## 性能测试

测试复用 openEuler 参考方案的两个基准，均在任务私有虚拟环境内运行：

### context_fs（文件系统操作矩阵）

挂载 `memfs` 后，对 `1KB`、`64KB`、`1MB` 三档负载分别测量
`write`、`read`、`stat`、`ls`、`rm`、`mkdir`、`grep` 七类操作，每类操作
执行 `OPENVIKING_ITERATIONS` 次（默认 3，可覆盖），保留平均延迟与吞吐：

- 每个操作：平均延迟（`ms`）与操作速率（`ops/s`）；
- `write`/`read` 额外保留数据吞吐（`MB/s`）。

### micro_operations（微基准）

- `import_init`：openviking 冷导入耗时与 `importlib.reload` 耗时；
- `client_init`：绑定客户端构造、`health()`、`get_capabilities()` 耗时；
- `fs_ops_latency`：`1KB`/`64KB`/`1MB` 负载下 `write`/`read`/`stat`/`ls`/`rm`
  的平均延迟、操作速率与 `write`/`read` 吞吐；
- `multithread_fs_ops`：`1`、`2`、`4`、`8`、`32` 线程并发写 64KB 数据
  （总计 50 次操作），保留合计操作速率与平均单操作延迟。

不绑定 CPU，由 Runner 按其正常调度策略执行。任何测量失败、指标为空/
非正/非有限，都会使测试失败。

可以脱离 Workflow 执行完整流程；脚本会依次执行 build、start、test、stop
并保存同样的产物。例如：

```bash
bash software/AI/openviking/openviking_test.sh \
  --version 0.4.17.1 \
  --results-dir /home/runner/boostkit-perf/openviking/results/0.4.17.1
```

## 指标

保留各“负载 × 操作”组合的原始测量值，不做平均、加权、评分或跨操作
聚合。因此总计为：

```text
context_fs: 3 个负载 × (7 个操作 × 2 个指标 + 2 个吞吐) = 48 个指标
micro import_init: 2 个指标
micro client_init: 3 个指标
micro fs_ops: 3 个负载 × (5 个操作 × 2 个指标 + 2 个吞吐) = 36 个指标
micro multithread: 5 个线程档位 × 2 个指标 = 10 个指标
共 99 个指标
```

| 指标 | 报告中的指标名格式 | 单位 | 优化方向 | 含义 |
|---|---|---|---|---|
| 操作速率 | `context_fs <负载> <操作>_ops_per_sec` | ops/s | 越大越好 | 平均每秒完成的文件系统操作数 |
| 操作延迟 | `context_fs <负载> <操作>_avg_latency_ms` | ms | 越小越好 | 单次操作平均耗时 |
| 数据吞吐 | `context_fs <负载> <write\|read>_throughput_mbs` | MB/s | 越大越好 | 读/写数据吞吐量 |
| 微基准延迟 | `micro import_init/client_init <阶段>_ms` 等 | ms | 越小越好 | 导入/初始化/健康检查耗时 |
| 微基准 fs 指标 | `micro fs_ops <负载> <操作>_*` | ms / ops/s / MB/s | 见方向列 | 与 context_fs 同口径的微基准测量 |
| 并发吞吐 | `micro multithread threads_<线程数> combined_ops_per_sec` | ops/s | 越大越好 | 多线程并发写的合计操作速率 |
| 并发延迟 | `micro multithread threads_<线程数> avg_per_op_latency_ms` | ms | 越小越好 | 多线程并发写的平均单操作延迟 |

报告按 `context_fs`、`import`、`client_init`、`fs_ops`、`multithread`
分组展示，便于在相同负载下比较 x86_64 与 aarch64。测试工具及其固定版本
会同时列在报告的“测试环境”部分。

## 结果与清理

`case.yaml` 要求以下测试产物：

- `benchmark_openviking_raw.log`：两个基准与聚合的完整控制台输出；
- `benchmark_context.json`：context_fs 基准的结构化结果；
- `micro_benchmark.json`：微基准的结构化结果；
- `results.json`：99 个结构化指标及其分组与来源字段。

`stop` 阶段删除本次任务创建的虚拟环境目录。Framework 随后执行 Runner
级环境清理。
