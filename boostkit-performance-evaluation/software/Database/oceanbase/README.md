# OceanBase 性能测试说明

本目录基于 OceanBase 官方社区版部署单机演示集群，并使用 `sysbench` 执行 OLTP
基准测试与微基准测试。OceanBase 属于“部署服务型”软件（区别于源码编译型），
Framework 的四阶段分别是：部署 observer（`obd demo`）、确认服务可用、执行
`sysbench` 基准、销毁测试集群并清理资源。直接执行 `oceanbase_test.sh` 也复用相同
阶段，并额外生成单架构报告。

部署由 [OceanBase Deployer（obd）](https://github.com/oceanbase/obd) 的 `demo`
命令完成，通过任务私有 `OBD_HOME`、部署路径与独立端口保证各运行之间相互隔离，
不会连接或复用已有的 observer。

## 部署（build）

`build_oceanbase` 阶段安装依赖（OceanBase 软件源、`ob-dploy`、`mysql` 客户端、
`sysbench`）后，在任务私有目录部署指定版本的 observer：

```bash
obd demo -c oceanbase-ce \
  --oceanbase-ce.version="${SOFTWARE_VERSION}" \
  --oceanbase-ce.mysql_port="${OB_PORT}" \
  --oceanbase-ce.rpc_port="${OB_RPC_PORT}" \
  --home_path="${OB_DEPLOY_HOME}"
```

部署完成后脚本通过 mysql 协议恒等 `SELECT 1` 探测观察进程就绪，并通过
`SELECT version()` 读取实际版本，必须与请求版本完全一致，否则构建失败。缺失依赖
由脚本通过 `dnf` 或 `sudo -n dnf` 自动安装。

observer 数据与日志位于任务工作目录（默认 `/home/runner/boostkit-perf/oceanbase/local-<运行 ID>`）
而非系统目录，`stop` 阶段只销毁本运行创建的 `demo` 集群并删除该精确目录。

## 服务启动（start）

`start_oceanbase_service` 阶段通过 `mysql` 协议确认 `${OB_HOST}:${OB_PORT}` 上的
observer 已就绪并可执行 `SELECT 1`；不可达则失败。

## 性能测试（test）

`run_oceanbase_benchmarks` 阶段在 observer 就绪后依次执行：

1. **OLTP 基准**（`benchmark_ob.py`）：4 个 workload × 4 个线程级别，通过
   `sysbench` 执行 `oltp_point_select`、`oltp_read_write`、`oltp_read_only`、
   `oltp_write_only`，线程级别为 1/4/16/32。
2. **微基准**（`micro_benchmark.py`）：`oltp_point_select` 的线程扩展
   （1/4/8/16/32/64）与表数量扩展（1/4/10）。
3. **结果聚合**（`aggregate_results.py`）：读取前两步 JSON，产出 Framework 可消费
   的 `results.json`。

每个 workload/线程组合运行 `iterations` 次，对 `tps`、`qps`、`avg_latency_ms`、
`p95_latency_ms` 取平均。非正值或非有限的指标会被跳过；若最终没有任何有效指标，
测试失败。默认参数为 `tables=4`、`table_size=10000`、`time_per_test=60`、
`iterations=1`，不绑定 CPU。

## 指标

指标聚合自 OLTP 与微基准原始输出，形成以下结构化指标（最多 82 项；非正值会被
跳过），不做评分或跨场景比例聚合：

| 来源 | 指标 | 单位 | 优化方向 |
|---|---|---|---|
| OLTP（4 负载 × 4 线程） | `<workload>: threads_<n> tps` | trans/sec | 越大越好 |
| OLTP | `<workload>: threads_<n> qps` | queries/sec | 越大越好 |
| OLTP | `<workload>: threads_<n> avg_latency_ms` | ms | 越小越好 |
| OLTP | `<workload>: threads_<n> p95_latency_ms` | ms | 越小越好 |
| 微基准（线程扩展） | `thread_scaling: threads_<n> {qps,p95_latency_ms}` | queries/sec / ms | 越大越好 / 越小越好 |
| 微基准（表数量扩展） | `table_count_sweep: tables_<n> {qps,p95_latency_ms}` | queries/sec / ms | 越大越好 / 越小越好 |

`benchmark_ob.json` 与 `micro_benchmark.json` 保存结构化原始结果及负载参数；
`results.json` 保存严格校验后的结构化指标。

## 独立执行

```bash
bash software/Database/oceanbase/oceanbase_test.sh \
  --version 5.0.1.0 \
  --results-dir /home/runner/boostkit-perf/oceanbase/results/5.0.1.0
```

独立执行依次完成环境采集、部署、服务就绪确认、基准测试、集群销毁、资源清理、
结果校验和报告生成。默认会销毁 `demo` 集群并清理其自行创建的
`/home/runner/boostkit-perf/oceanbase/local-*` 目录；需要保留该目录排查部署问题时，追加
`--keep-workdir`。

## 清理（stop）

`stop_oceanbase_service` 阶段执行 `obd cluster destroy demo -f` 销毁本运行创建的
演示集群，并删除部署目录，不格式化磁盘或影响其他 OceanBase 实例。