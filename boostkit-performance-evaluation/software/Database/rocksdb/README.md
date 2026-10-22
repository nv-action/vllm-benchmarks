# RocksDB 6.26.1 性能测试

本用例比较上游原版 RocksDB 6.26.1 在 x86_64 和 aarch64 上的单机开箱性能。
测试方法参考《930RocksDB版本需求验收报告》的物理机单实例流程：YCSB 加载数据、
只读预热，然后运行 workload A、C。RocksDB 是进程内嵌入式库，不启动服务端。

测试不打 RocksDB 优化补丁，不改 YCSB 源码，不使用文档的 `dbsnappy*.ini`、
Docker、绑核、NUMA 绑定、`drop_caches` 或系统参数调优。没有传入
`rocksdb.optionsfile`；上游 YCSB RocksDB binding 自身的默认初始化逻辑保持不变。

## 构建与安装

脚本通过 `dnf` 自动安装缺失的编译工具、JDK 11、Maven，以及 RocksDB 所需开发包。
依赖只装系统软件源里的包；RocksDB 和 YCSB 源码、JNI JAR、Maven 仓库均在
`/home/runner/boostkit-perf/rocksdb/<本次运行>/` 下。不会覆盖系统 RocksDB、
系统 Java 或用户的 `~/.m2`。

上游来源固定为：

| 组件 | 版本或提交 | 来源 |
|---|---|---|
| RocksDB | `v6.26.1` | https://github.com/facebook/rocksdb |
| YCSB | `19e885f7cb780fdded0547853f7810a150554caf`，构建版本 `0.18.0-SNAPSHOT` | https://github.com/brianfrankcooper/YCSB |

脚本实际执行的关键命令如下；`WORK` 表示本次任务私有目录：

```bash
git clone --branch v6.26.1 --depth 1 \
  https://github.com/facebook/rocksdb.git "$WORK/rocksdb-source"
cd "$WORK/rocksdb-source"
PORTABLE=1 DEBUG_LEVEL=0 make rocksdbjava -j"$(nproc)" \
  DISABLE_WARNING_AS_ERROR=1 DISABLE_JEMALLOC=1

mvn -B -s "$WORK/maven-settings.xml" install:install-file \
  -Dfile="$WORK/rocksdb-source/java/target/rocksdbjni-6.26.1-linux64.jar" \
  -DgroupId=org.rocksdb -DartifactId=rocksdbjni \
  -Dversion=6.26.1 -Dpackaging=jar -DgeneratePom=true

git clone --filter=blob:none https://github.com/brianfrankcooper/YCSB.git \
  "$WORK/ycsb-source"
git -C "$WORK/ycsb-source" checkout --detach \
  19e885f7cb780fdded0547853f7810a150554caf
cd "$WORK/ycsb-source"
mvn -B -s "$WORK/maven-settings.xml" \
  -pl site.ycsb:rocksdb-binding -am clean package -DskipTests \
  -Drocksdb.version=6.26.1
```

`MAVEN_OPTS` 指向 `$WORK/maven-repository`，所以 YCSB 绑定使用本机刚构建的
RocksDB 6.26.1 JNI JAR，而不是其 POM 默认的 RocksDB 6.2.2。
构建出的 `ycsb-rocksdb-binding-0.18.0-SNAPSHOT.tar.gz` 解压至
`$WORK/ycsb-dist`，正式测试调用包内原版 `bin/ycsb`。绑定包将所有 JAR 放在
顶层 `lib/`；脚本会核对其中的 JNI JAR 与本机编译的 RocksDB JAR 完全一致。
如果配置了 `PERF_PROXY`，只给本次 Maven 调用生成私有代理配置；未配置时直连。

## 数据与测试

数据库和快照分别位于 `$WORK/data/workload_data`、
`$WORK/data/warmup_workload_data`。`$WORK` 在 `/home/runner` 持久磁盘上，
数据库数据不写入 `/tmp`。所有目录只属于本次运行，停止阶段仅清理本次私有数据目录。

| 阶段 | YCSB 命令 | 线程 | 用途 |
|---|---|---:|---|
| 加载 | `load rocksdb -P workloads/workloada` | 5 | 建立 1 亿条记录 |
| 预热 1 | `run rocksdb -P workloads/workloadc` | 3 | 只读预热，随后复制数据库快照 |
| 恢复与预热 2 | 恢复快照，再运行 `workloadc` | 3 | 按文档物理机流程准备正式测试 |
| 正式 A | `run rocksdb -P workloads/workloada` | 5 | 50% 读、50% 更新 |
| 正式 C | `run rocksdb -P workloads/workloadc` | 5 | 只读 |

每次调用均传入以下相同参数：

```bash
-s -p rocksdb.dir="$WORK/data/workload_data" \
  -p recordcount=100000000 -p operationcount=100000000 \
  -p fieldlength=256 -p fieldcount=1 \
  -p rocksdb.cfs=default -p table=default
```

没有设置 `target`，即不限制 YCSB 请求速率。A、C 顺序运行，与文档物理机脚本一致；
两架构使用同一命令参数。此流程执行数亿次操作并复制大规模数据库，可能运行数小时；
测试作业上限为 24 小时，不缩小文档数据量来换取短时间结果。

## 指标与结果

报告分别显示 workload A 和 C 的 `[OVERALL], Throughput(ops/sec)`，单位为
`ops/sec`，越大越好。加载和预热的吞吐量只保留在原始输出中，不当作正式成绩。
`ycsb_raw.log` 保存每条命令和完整输出；`results.json` 保存正式指标与负载参数。
任一命令失败、YCSB 报告失败操作，或缺少唯一、正数的吞吐量，测试即失败。

独立运行命令：

```bash
bash software/Database/rocksdb/rocksdb_test.sh \
  --version 6.26.1 \
  --results-dir /home/runner/boostkit-perf/rocksdb/results/6.26.1
```
