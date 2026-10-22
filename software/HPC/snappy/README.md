# Snappy 1.2.2 测试说明

本用例在 x86_64 和 aarch64 上分别构建 Google Snappy 1.2.2，使用 lzbench 2.2
执行 [BoostKit Snappy 快速入门](https://atomgit.com/boostkit/snappy/blob/master/docs/zh/quick_start.md#%E4%BD%BF%E7%94%A8%E7%A4%BA%E4%BE%8B%E4%BD%BF%E7%94%A8lzbench%E8%BF%9B%E8%A1%8C%E6%80%A7%E8%83%BD%E6%B5%8B%E8%AF%95)
中的测试命令。两台机器使用同一版本、同一份 Silesia 数据和同一组参数。

## 构建和安装

脚本入口为 `snappy_test.sh`。它检查依赖命令，缺失时通过 `dnf` 安装；非 root
用户使用 `sudo -n dnf`。源码和构建结果仅存于本次任务的
`/home/runner/boostkit-perf/snappy/` 子目录，不安装到系统路径。

核心构建命令如下；实际绝对路径由 Framework 为每次运行生成：

```bash
git clone --branch 1.2.2 --depth 1 https://github.com/google/snappy.git snappy-source
cmake -S snappy-source -B snappy-source/build \
  -DCMAKE_BUILD_TYPE=Release -DBUILD_SHARED_LIBS=ON \
  -DSNAPPY_BUILD_TESTS=OFF -DSNAPPY_BUILD_BENCHMARKS=OFF
cmake --build snappy-source/build

git clone --branch v2.2 --depth 1 https://github.com/inikep/lzbench.git lzbench-source
make -C lzbench-source -j4 BUILD_STATIC=0 DONT_BUILD_DENSITY=1 \
  SNAPPY_FILES= \
  USER_LDFLAGS="-L<本次运行的 snappy-source/build> -Wl,-rpath,<本次运行的 snappy-source/build> -lsnappy"
```

`SNAPPY_FILES=` 排除 lzbench 自带的 Snappy 实现；构建后检查 `ldd`，确保
`lzbench` 连接的是本次构建的 Snappy 1.2.2。`DONT_BUILD_DENSITY=1` 只排除
与本测试无关、构建时需要 Rust 依赖的 Density 编解码器。

## 数据与测试

脚本优先读取 `/home/runner/software/snappy/silesia.tar`；文件不存在时，从
`https://wanos.co/assets/silesia.tar` 下载到本次私有工作目录。使用前验证其
SHA-256 为 `ea122ed051dc7a6c58d2bb56bb05b34d9f1537c4dc9e71519142e2ca8cd6338d`。
校验失败则不运行测试。

测试命令为：

```bash
./lzbench -esnappy -b4 -t20u20 silesia.tar
```

其中 `-esnappy` 选择 Snappy，`-b4` 使用 4 KiB 数据块，`-t20u20`
分别给压缩和解压约 20 秒的测量时间。`start` 阶段验证工具；`test` 阶段
准备数据并运行命令；`stop` 阶段无需停止后台服务。

可在仓库根目录独立运行完整流程：

```bash
bash software/HPC/snappy/snappy_test.sh \
  --version 1.2.2 \
  --results-dir /home/runner/boostkit-perf/snappy/results/1.2.2
```

## 指标与输出

`benchmark_lzbench.txt` 保存完整原始输出，`benchmark_snappy.json` 保存解析后的
数值及命令、数据校验值。报告列出：

| 指标 | lzbench 原始列 | 单位 | 优化方向 |
|---|---|---|---|
| 压缩吞吐 | `Compress.` | MB/s | 越大越好 |
| 解压吞吐 | `Decompress.` | MB/s | 越大越好 |
| 压缩后大小比例 | `Ratio` | % | 越小越好 |

只有完整的单行 Snappy 结果且运行参数与请求一致时才生成结构化指标。独立运行
另会生成环境、构建、状态和报告文件；Framework 负责跨架构结果汇总。
