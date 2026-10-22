# Protobuf 33.0 性能测试

入口为 `protobuf_test.sh`，Framework 阶段与输出文件见 `case.yaml`。两架构均从 [Protobuf 上游仓库](https://github.com/protocolbuffers/protobuf)构建未打补丁的 `v33.0`，运行 [AccLibBenchmark 的 protobuf-benchmark](https://gitcode.com/boostkit/AccLibBenchmark/tree/master/protobuf-benchmark) 原 C++ 测试用例，测试源码固定在提交 `01d083c6633babb2fb1307d93f596f10dda36778`。

## 源码与构建

所有源码、构建目录和安装目录都位于本次任务的 `PERF_WORK_DIR`；不会替换系统中的 Protobuf。脚本先以 CMake Release 配置构建、安装 Protobuf，并校验私有 `protoc --version`：

```bash
git clone --branch v33.0 --depth 1 https://github.com/protocolbuffers/protobuf.git protobuf-source
cmake -S protobuf-source -B build -DCMAKE_BUILD_TYPE=Release \
  -Dprotobuf_BUILD_TESTS=OFF -Dprotobuf_BUILD_SHARED_LIBS=OFF \
  -Dprotobuf_FORCE_FETCH_DEPENDENCIES=ON \
  -DCMAKE_INSTALL_PREFIX=<本次任务的 install 目录>
cmake --build build --parallel "$(nproc)"
cmake --install build
<本次任务的 install 目录>/bin/protoc --version
```

`protobuf_FORCE_FETCH_DEPENDENCIES=ON` 使 Protobuf 使用上游 v33.0 锁定的 Abseil 20250512.1，而非 Runner 上可能存在的旧版 Abseil。依赖通过上游 CMake 下载、构建，并随 Protobuf 安装到本次任务私有目录；需要能访问 GitHub。

测试源码优先从 `/home/runner/software/protobuf/AccLibBenchmark` 读取；也可通过脚本环境变量 `PROTOBUF_BENCHMARK_REPO` 指定已有 Git 仓库。缺失时从 GitCode 克隆，随后检出上述固定提交。私有构建还从 [Google Benchmark](https://github.com/google/benchmark) 获取 `v1.8.3` 并安装在本次任务目录。

原测试仓库的 CMake 文件固定了 ARM SVE2 指令并手写 Abseil 库清单。为适配两种架构，脚本使用 `scripts/benchmark_project/CMakeLists.txt` 编译原仓库的 `.proto` 和 `benchmark_*.cpp`，通过两套私有安装的 CMake package 链接 Protobuf 与 Google Benchmark；不为任一架构强制专用指令。产物为本次任务 `benchmark-build/bm`。

构建前，`scripts/fix_fixtures.py` 只修改本次任务复制的测试工具头文件 `benchmark_common.h`：18 个随机生成器按数据类型和元素数独立使用固定种子；原已固定种子 42 的 String、Bytes 保持不变。改动不涉及 Protobuf 源码，也不改变原 168 个用例及测量循环。实际测试源码差异保存在 `benchmark_source.diff`。

## 测试与指标

原 `bm` 共注册 168 个带参数用例，覆盖 Scalar、Repeated、String、Bytes 的序列化和反序列化。每项分别使用大小 `10`、`100`、`1000`、`10000`。脚本先列出并核对 168 个用例，再运行：

```bash
./bm --benchmark_min_time=1s --benchmark_repetitions=5 \
  --benchmark_display_aggregates_only=true \
  --benchmark_out=<RESULTS_DIR>/benchmark_google.json \
  --benchmark_out_format=json
```

测试前用同一生成器生成 84 份不同类型及大小的输入，再生成一遍核对本机可重复性；SHA-256 清单保存为 `fixture_sha256.txt`。两架构跑完后，先对比清单：

```bash
diff -u <x86_64 结果目录>/fixture_sha256.txt \
  <aarch64 结果目录>/fixture_sha256.txt
```

清单不同表示两边输入字节不一致，不应解读跨架构性能差异。该清单覆盖每个原始用例所调用的数据生成函数；同一函数每次按类型及大小重新播种，因此编码、解码及五次重复不受执行顺序影响。

原始 JSON、控制台日志和用例清单都保留在 `RESULTS_DIR`。`scripts/parse_google_benchmark.py` 检查每项都有 5 次有效测量，取 `cpu_time` 的中位数并统一换算为 `ns/op`，生成 `benchmark_protobuf.json`。报告将 168 项分为六张表：Scalar 非 packed、Repeated packed、String 与 Bytes 各自区分序列化和反序列化。每行保留原始用例名和参数大小；同名用例跨架构对齐，数值越低越好，不跨类别求总分。测试不绑核。

固定一组输入有助于排除输入差异，但不代表所有业务数据分布；细小差距仍须结合测量波动判断。

独立运行：

```bash
bash software/HPC/protobuf/protobuf_test.sh --version 33.0 \
  --results-dir /home/runner/protobuf-results
```
