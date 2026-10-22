# Redis 性能测试说明

本目录从 Redis 官方仓库构建指定版本，启动一个任务私有 Redis 实例，使用
Redis 自带的 `redis-benchmark` 测试 40、60、80 三档并发下的 SET/GET 吞吐量。
Framework 通过 `case.yaml` 调用 `redis_test.sh` 的 `build`、`start`、`test`、
`stop` 四个阶段；直接执行入口脚本会使用同一套构建、测试、信息收集和清理流程。

当前测试版本为 `7.0.15`，独立入口也默认使用该版本。

## 构建与安装

Redis 从官方 GitHub 标签浅克隆，在任务隔离工作目录中编译，不安装系统级 Redis
软件包。脚本检查 `git`、`python3`、`gcc`、`make`、`sed`、`grep` 和 coreutils
工具；缺失时通过 `dnf` 自动安装，非 root Runner 使用 `sudo -n dnf`。

当前实际构建命令为：

```bash
git clone --branch 7.0.15 --depth 1 https://github.com/redis/redis.git redis-source
cd redis-source
make -j"$(nproc)" BUILD_TLS=no
```

构建完成后必须存在以下 Redis 官方二进制，并通过 `redis-server --version` 校验实际
版本与请求版本一致：

```text
src/redis-server
src/redis-benchmark
src/redis-cli
```

源码与编译产物位于 `/home/runner/boostkit-perf/redis/` 下的本次任务工作目录。
独立执行结束后默认清理构建工作目录；使用 `--keep-workdir` 可保留它用于排障。

## 服务启动与数据目录

Redis 服务数据不放在 `/tmp`。每次运行使用唯一的持久化目录：

```text
${PERF_WORK_DIR}/data/
```

该目录包含 Redis 的数据文件、RDB/AOF（如运行期间产生）、日志和 PID。可通过
`REDIS_DATA_ROOT` 修改根目录。端口默认由运行 ID 稳定派生，范围为
`20000`–`39999`，也可通过 `REDIS_SERVICE_PORT` 显式指定，从而不会固定占用 6379
或与并发任务冲突。

核心启动命令如下：

```bash
./src/redis-server \
  --bind 127.0.0.1 \
  --protected-mode yes \
  --port "${REDIS_SERVICE_PORT}" \
  --dir "${REDIS_DATA_ROOT}" \
  --daemonize yes \
  --pidfile "${SERVICE_DIR}/redis.pid" \
  --logfile "${SERVICE_DIR}/redis.log"

./src/redis-cli -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" PING
```

脚本拒绝复用既有任务数据目录或既有端口服务。Redis 使用默认持久化行为，未通过
`--save ""` 或 `--appendonly no` 关闭 RDB/AOF。

## 性能测试

服务端和客户端均在本机，连接 `127.0.0.1`。每档开始前，仅对本次任务私有实例
执行 `FLUSHALL`，避免上一档留下的数据影响下一档。正式测试依次执行：

```bash
for clients in 40 60 80; do
  ./src/redis-cli -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" FLUSHALL
  ./src/redis-benchmark -h 127.0.0.1 -p "${REDIS_SERVICE_PORT}" \
    -c "${clients}" -d 3 -n 10000000 -r 10000000 \
    -t set,get --threads 20 -q
done
```

参数含义如下：

| 参数 | 值 | 含义 |
|---|---:|---|
| `-c` | 40、60、80 | 依次测试的并发客户端连接数 |
| `-d` | 3 | SET/GET 值的字节数 |
| `-n` | 10,000,000 | 每项操作、每档并发的请求数 |
| `-r` | 10,000,000 | 随机键空间大小 |
| `-t` | `set,get` | 只运行 SET 和 GET |
| `--threads` | 20 | benchmark 客户端线程数 |
| `-q` | — | 只输出吞吐量摘要 |

三档合计执行 6 个操作测试；整套最长允许 14,400 秒。任何一档缺少唯一的 SET 或
GET 吞吐摘要、吞吐为非正数，或命令执行失败，测试都直接失败。完整原始输出及
每档实际命令保存在 `redis_benchmark_raw.log`。

## 指标

只取原始输出中的 `requests per second` 数值，不统计延迟、不跨操作或并发档位求平均。

| 报告分组 | 操作 | 原始输出字段 | 单位 | 优化方向 |
|---|---|---|---:|---|
| 40 并发 | SET、GET | `<操作>: <值> requests per second` | requests/s | 越大越好 |
| 60 并发 | SET、GET | `<操作>: <值> requests per second` | requests/s | 越大越好 |
| 80 并发 | SET、GET | `<操作>: <值> requests per second` | requests/s | 越大越好 |

`benchmark_redis.json` 保存 6 项结构化吞吐与固定测试参数。报告按并发档位分组，
同一档只对比同一个操作的两架构数值。

## 独立执行、结果与清理

可脱离 Workflow 执行完整流程：

```bash
bash software/Database/redis/redis_test.sh \
  --version 7.0.15 \
  --results-dir /home/runner/boostkit-perf/redis/results/7.0.15
```

独立执行会依次完成环境采集、构建、启动、测试、停止、结果校验和单架构报告生成。
结果目录至少包含：

- `redis_benchmark_raw.log`：三条压测命令和完整原始输出；
- `benchmark_redis.json`：六项 SET/GET 吞吐及测试参数；
- `system_info.json`、`runtime_before.json`、`runtime_after.json`：测试环境信息；
- `build_info.json`、`results.json`、`status.json`、`report.md`：构建记录、校验结果和报告。

停止阶段先使用任务端口执行 `SHUTDOWN NOSAVE`，必要时再终止记录的进程；确认端口
不可达后，仅删除本次运行 ID 的 Redis 数据目录。独立运行还会删除自己创建的构建
工作目录，除非指定 `--keep-workdir`。
