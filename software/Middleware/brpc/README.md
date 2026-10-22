# brpc 1.17.0 性能测试

测试入口为 `brpc_test.sh`；Framework 的构建、启动、测试和停止阶段见 `case.yaml`。两架构均构建 [Apache brpc 1.17.0 原版源码](https://github.com/apache/brpc/tree/1.17.0)，运行其 `example/rdma_performance` 的原版 `server.cpp`、`client.cpp`，不修改待测 C++ 源码。

## 构建与安装

所有源码、构建产物和服务文件位于本次任务私有的 `PERF_WORK_DIR`，不安装系统级 brpc。缺少的命令及开发包由脚本通过 `dnf` 安装，包括 `rdma-core-devel`。原版示例即使以 TCP 模式运行，构建时也需要 `WITH_RDMA=ON` 及 ibverbs 开发库；运行时 `--use_rdma=false`，不使用 RDMA 设备。

```bash
git clone --branch 1.17.0 --depth 1 https://github.com/apache/brpc.git brpc-source
cmake -S brpc-source -B brpc-source/build \
  -DCMAKE_BUILD_TYPE=Release -DWITH_DEBUG_SYMBOLS=OFF \
  -DWITH_RDMA=ON -DBUILD_BRPC_TOOLS=OFF
cmake --build brpc-source/build -j "$(nproc)"

cmake -S brpc-source/example/rdma_performance \
  -B brpc-source/example/rdma_performance/build -DCMAKE_BUILD_TYPE=Release
cmake --build brpc-source/example/rdma_performance/build -j "$(nproc)" \
  --target client --target server
```

原示例的独立 CMake 文件针对较旧的 Protobuf/C++11 编写。脚本只在本次任务副本中调整其 CMake 配置：启用 C++17，补齐当前 Protobuf 的 Abseil 依赖以及 ibverbs、zlib 链接；不改原版 `server.cpp`、`client.cpp`、`test.proto`。产物是示例目录 `build/server` 和 `build/client`。

## 测试

`start` 阶段在本机启动服务端。`test` 阶段按照参考文档的消息大小执行 0 B、1 K、4 K、8 K、100 K、200 K、1 M、8 M 八个 TCP、`baidu_std` attachment 回显场景。每个场景重复 5 次，每次 20 秒；至少需要约 13 分 20 秒，另加构建和启动时间。以下以 1 K 为例：

```bash
./server \
  --use_rdma=false \
  --port=8003 \
  --bthread_concurrency=32

./client \
  --use_rdma=false \
  --protocol=baidu_std \
  --connection_type=single \
  --servers=127.0.0.1:8003 \
  --thread_num=32 \
  --queue_depth=32 \
  --bthread_concurrency=160 \
  --attachment_size=1024 \
  --echo_attachment=true \
  --test_seconds=20
```

原版 `server.cpp` 没有定义 `--server_num_threads`，因此不传该参数；服务端的 `--bthread_concurrency=32` 仍按要求设置。客户端的默认 dummy port 为 8001，脚本会在运行前检查 8001 和服务端 8003 端口是否可用。测试不绑核，不增加预热轮次；`stop` 阶段关闭服务端。

八种大小为 `0 1024 4096 8192 102400 204800 1048576 8388608` 字节。原示例的 32 线程、32 队列深度在大附件场景会积压过多请求并触发 `E1011`。因此按附件大小控制最多 32 MiB 请求附件同时在途；两架构使用同一参数：

| 附件大小 | `--thread_num` | `--queue_depth` |
|---:|---:|---:|
| 0、1024、4096、8192 B | 32 | 32 |
| 102400 B | 32 | 10 |
| 204800 B | 32 | 5 |
| 1048576 B | 32 | 1 |
| 8388608 B | 4 | 1 |

仍运行原版客户端和服务端，不修改 C++ 源码；每档实际参数写入客户端日志和结果 JSON。若任一场景仍出现 RPC 失败或客户端异常退出，测试直接失败，不用该次结果生成报告。

也可以独立运行完整流程：

```bash
bash software/Middleware/brpc/brpc_test.sh \
  --version 1.17.0 \
  --results-dir /home/runner/boostkit-perf/brpc/results/1.17.0
```

## 指标与结果

40 次原客户端输出保存在 `benchmark_client.log`。解析器核对每种大小恰有 5 次完整摘要，将各指标的 5 个原始数值及平均值写入 `benchmark_brpc.json`；报告按 attachment 大小分组。若客户端出现 RPC 失败、缺少摘要或场景参数不匹配，测试失败。

| 指标 | 原版输出字段 | 单位 | 优化方向 |
|---|---|---|---|
| `qps` | `QPS` | requests/s | 越大越好 |
| `throughput` | `Throughput` | MB/s | 越大越好 |
| `avg_latency` | `Avg-Latency` | us | 越小越好 |
| `p90_latency` | `90th-Latency` | us | 越小越好 |
| `p99_latency` | `99th-Latency` | us | 越小越好 |
| `p999_latency` | `99.9th-Latency` | us | 越小越好 |

原版客户端用整数 `k` 输出 QPS，8 M 等大消息的非零 QPS 可能显示为 `0k`。对于非零 attachment，解析器根据同一原始摘要的吞吐量计算 QPS（`MB/s × 1048576 ÷ attachment 字节数`）；0 B 场景按原客户端 QPS 的 `k` 单位乘以 1000。原始摘要及每次结果均保留。延迟来自客户端 `LatencyRecorder` 摘要。独立运行还保存 `results.log`、环境信息和单架构报告。
