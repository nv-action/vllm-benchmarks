# Flink 性能测试说明

本目录部署 Apache Flink `2.3.0` 的官方二进制发行包，在一台 Runner 上启动一个 JobManager 与一个 TaskManager，并提交有限的 DataStream 作业。软件入口为 `flink_test.sh`；`case.yaml` 将其 `build`、`start`、`test`、`stop` 四个函数分别暴露给 Framework。直接执行入口脚本时也使用相同的安装、部署、测试、信息收集和清理过程。

当前只包含一个短时间的链路验证场景：1,000,000 条内存序列数据经无状态 `map` 后写入 `DiscardingSink`。它用于验证官方发行包、单机集群、作业提交、REST 指标采集和清理流程；不测状态后端、checkpoint、外部存储、消息队列或网络集群性能。

## 工具、版本与文件来源

被测软件是 Apache Flink `2.3.0`，使用 Scala `2.12` 的官方二进制发行包：

| 用途 | 文件名 | 官方地址 | SHA-512 |
|---|---|---|---|
| Flink Standalone 发行包 | `flink-2.3.0-bin-scala_2.12.tgz` | `https://downloads.apache.org/flink/flink-2.3.0/flink-2.3.0-bin-scala_2.12.tgz` | `e5863767caeaa7c72e45fc62d45f7df9f435a1c83aed813ea550db39e9221194d148ea4a6c3bfb5604335974729c579d48c6c4c3eb43502e37310a0bf982462a` |

二进制包与架构无关；同一个包可用于 x86_64 与 aarch64，实际 JVM 由各 Runner 上的 Java 决定。Flink 运行时要求 Java `11`、`17` 或 `21`。脚本会检查 `java`、`javac`、`jar`、`curl`、`tar`、`gzip`、`sha512sum` 和 Python 3；缺失时通过 `dnf` 安装系统依赖：

```bash
sudo dnf install -y curl tar gzip coreutils python3 java-17-openjdk-devel
```

脚本不会将 Flink 安装到系统路径，也不会修改 Runner 已有的 Flink 服务。

## 离线包与下载

脚本按以下顺序获取发行包：

1. `/home/runner/software/flink/flink-2.3.0-bin-scala_2.12.tgz`；
2. Apache 官方下载地址。

无论来源如何，都会对文件执行下列等价校验；SHA-512 不匹配立即失败，不解压也不启动服务：

```bash
sha512sum flink-2.3.0-bin-scala_2.12.tgz
```

在 Runner 上预置离线包的完整步骤如下：

```bash
sudo install -d -o runner -g runner /home/runner/software/flink
sudo install -o runner -g runner flink-2.3.0-bin-scala_2.12.tgz \
  /home/runner/software/flink/flink-2.3.0-bin-scala_2.12.tgz
sha512sum /home/runner/software/flink/flink-2.3.0-bin-scala_2.12.tgz
```

Framework 运行时，包解压到本次任务私有目录：

```text
${PERF_WORK_DIR}/flink
```

直接执行入口脚本时，默认工作目录为：

```text
/home/runner/boostkit-perf/flink/local-<运行ID>/flink
```

解压和版本验证的实际命令形态为：

```bash
tar -xzf "${PERF_WORK_DIR}/flink-2.3.0-bin-scala_2.12.tgz" \
  -C "${PERF_WORK_DIR}/flink" --strip-components=1
"${PERF_WORK_DIR}/flink/bin/flink" --version
```

`flink --version` 的输出必须包含请求版本 `2.3.0`，随后记录到 `actual-version.txt` 与 Framework 的 `build_info.json`。

## 单机集群部署

每个运行 ID 都有独立的 Flink 运行目录：

```text
/home/runner/boostkit-perf/flink/<版本>/<架构>/<运行ID>/runtime/
├── logs/
└── pids/
```

`start` 阶段覆盖本次私有 Flink 安装目录中的 `conf/config.yaml`，只写入本次单机集群需要的配置：

```yaml
jobmanager.rpc.address: 127.0.0.1
jobmanager.bind-host: 127.0.0.1
jobmanager.rpc.port: <本次任务 RPC 端口>
taskmanager.bind-host: 127.0.0.1
taskmanager.rpc.port: <本次任务 TaskManager RPC 端口>
taskmanager.data.port: 0
rest.address: 127.0.0.1
rest.bind-address: 127.0.0.1
rest.port: <本次任务 REST 端口>
blob.server.port: <本次任务 Blob 端口>
taskmanager.numberOfTaskSlots: 1
parallelism.default: 1
jobmanager.memory.process.size: 1024m
taskmanager.memory.process.size: 1024m
env.log.dir: ${PERF_WORK_DIR}/runtime/logs
env.pid.dir: ${PERF_WORK_DIR}/runtime/pids
```

四个端口由运行 ID 稳定派生，分别位于 `20000–29999`、`30000–39999`、`40000–49999` 与 `50000–59999`，不会占用默认 REST `8081` 或默认 RPC `6123`。

启动顺序固定如下：

```bash
"${FLINK_HOME}/bin/jobmanager.sh" start
GET "${FLINK_REST_URL}/overview"
"${FLINK_HOME}/bin/taskmanager.sh" start
```

脚本最多等待 30 秒，只有 REST `GET /overview` 返回 HTTP 200 后才启动 TaskManager 和进入测试。服务只绑定 `127.0.0.1`；不使用 Docker、YARN、Kubernetes 或 SSH，不会访问其他主机。

## 测试作业与执行命令

`src/main/java/org/boostkit/performance/flink/PassThroughJob.java` 是唯一提交的作业。其逻辑为：

```text
fromSequence(1, 1,000,000) → map(value -> value) → DiscardingSink
```

它显式设置并行度为 `1`，输入数量为 `1,000,000`。没有状态算子、状态后端、checkpoint、外部 source/sink 或磁盘数据文件。`DiscardingSink` 只消费记录，不把测试数据写入日志或文件。

测试前，脚本使用发行包自身的 `lib/*` 编译并打包该 Job：

```bash
javac -cp "${FLINK_HOME}/lib/*" -d "${PERF_WORK_DIR}/job-classes" \
  "${SCRIPT_DIR}/src/main/java/org/boostkit/performance/flink/PassThroughJob.java"

jar --create --file "${PERF_WORK_DIR}/flink-pass-through-job.jar" \
  --main-class org.boostkit.performance.flink.PassThroughJob \
  -C "${PERF_WORK_DIR}/job-classes" .
```

测试阶段提交的完整命令形态为：

```bash
"${FLINK_HOME}/bin/flink" run -d \
  -c org.boostkit.performance.flink.PassThroughJob \
  "${PERF_WORK_DIR}/flink-pass-through-job.jar" \
  --records 1000000 \
  --parallelism 1
```

`-d` 使 Flink CLI 在取得 Job ID 后退出。适配脚本从 CLI 原始输出提取 Job ID，再轮询 REST `GET /jobs/<JobID>`，仅在作业状态为 `FINISHED` 时继续；`FAILED`、`CANCELED`、`SUSPENDED`、120 秒未结束或未找到 Job ID 都使测试阶段失败。

## 指标、原始数据与校验

当前场景只展示一个指标，不对多个算子、线程档位或场景做筛选、平均或评分：

| 场景 | 原始 Flink 字段 | 计算方式 | 报告指标 | 单位 | 优化方向 |
|---|---|---|---|---|---|
| 无状态直通流 | `Discard sink` vertex 的 `numRecordsIn` | `numRecordsIn / (end-time - start-time)` | `Discard sink numRecordsIn per second` | records/s | 越大越好 |

指标采集顺序为：

```bash
GET "${FLINK_REST_URL}/jobs/<JobID>"
GET "${FLINK_REST_URL}/jobs/<JobID>/vertices/<VertexID>/metrics?get=numRecordsIn"
```

脚本要求只有一个 vertex 名称包含 `Discard sink`，且其 `numRecordsIn` 必须严格等于 `1,000,000`。作业起止时间必须存在且持续时间大于零；任一条件不满足即失败，不产生空指标或部分通过结果。

Framework 要求以下结果：

| 产物 | 内容 |
|---|---|
| `raw-output.log` | `flink run` 提交命令及 CLI 原始输出，由 Framework 在 `test` 阶段保存。 |
| `flink-logs/` | 本次 JobManager 与 TaskManager 产生的原始日志副本。 |
| `benchmark_flink.json` | 固定参数、原始 REST Job 响应、原始 `numRecordsIn`、作业时长和唯一结构化指标。 |
| `build_info.json` | 请求版本、实际版本、架构及构建信息记录时间。 |
| `system_info.json` | Runner 系统、CPU、编译器和 glibc 信息。 |

## 停止与清理

无论测试成功或失败，独立入口都会执行停止阶段。停止命令为：

```bash
"${FLINK_HOME}/bin/taskmanager.sh" stop-all
"${FLINK_HOME}/bin/jobmanager.sh" stop
```

脚本随后只删除本次任务的 `${PERF_WORK_DIR}/runtime/`。不会停止非本次任务启动的服务，也不会删除 `/home/runner/software/flink/` 中的离线包。Framework 任务完成后再清理其私有 `${PERF_WORK_DIR}`。

## 独立执行

在仓库根目录执行完整流程：

```bash
bash software/Bigdata/flink/flink_test.sh --version 2.3.0
```

默认结果目录：

```text
software/Bigdata/flink/results/2.3.0/local-<UTC时间>-<PID>/
```

可指定结果目录：

```bash
bash software/Bigdata/flink/flink_test.sh \
  --version 2.3.0 \
  --results-dir /home/runner/boostkit-perf/flink/results/2.3.0
```

如需保留 `${PERF_WORK_DIR}` 排查下载、解压、编译或集群启动问题：

```bash
bash software/Bigdata/flink/flink_test.sh --version 2.3.0 --keep-workdir
```
