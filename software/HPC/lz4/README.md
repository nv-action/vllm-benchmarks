# LZ4 1.9.4 测试说明

本用例在 x86_64 和 aarch64 上使用同一份 Silesia 数据、同一组 lzbench 参数，
测试 LZ4 1.9.4 的默认压缩与解压性能。Framework 依次调用
`lz4_test.sh` 的 `build`、`start`、`test`、`stop` 函数。

## 构建和安装

脚本缺少依赖时通过 `dnf` 安装；非 root 用户使用 `sudo -n dnf`。源码和构建
结果只存于本次运行的 `/home/runner/boostkit-perf/lz4/` 子目录，不安装到系统路径。

```bash
git clone --branch v1.9.4 --depth 1 https://github.com/lz4/lz4.git lz4-source
make -C lz4-source/lib -j4

git clone --branch v2.2 --depth 1 https://github.com/inikep/lzbench.git lzbench-source
make -C lzbench-source -j4 BUILD_STATIC=0 DONT_BUILD_DENSITY=1 \
  LZ4_FILES= \
  USER_LDFLAGS="-L<本次运行的 lz4-source/lib> -Wl,-rpath,<本次运行的 lz4-source/lib> -llz4"
```

`LZ4_FILES=` 排除 lzbench 自带的 LZ4 1.10.0；构建后检查 `ldd`，确保
`lzbench` 加载本次构建的 `liblz4.so.1.9.4`。lzbench v2.2 的编解码器名称
原本硬编码为 `lz4 1.10.0`，脚本只把私有构建里的这一处显示文字改为
`lz4 1.9.4`，不修改压缩算法或测试参数。`DONT_BUILD_DENSITY=1` 只跳过
与本测试无关的 Rust Density 编解码器。

## 数据与测试

脚本优先读取 `/home/runner/software/lz4/silesia.tar`；若不存在，则从
`https://wanos.co/assets/silesia.tar` 下载到本次私有工作目录。测试前核验
SHA-256：`ea122ed051dc7a6c58d2bb56bb05b34d9f1537c4dc9e71519142e2ca8cd6338d`。

测试命令沿用 [lzbench 使用示例](https://atomgit.com/boostkit/snappy/blob/master/docs/zh/quick_start.md#%E4%BD%BF%E7%94%A8%E7%A4%BA%E4%BE%8B%E4%BD%BF%E7%94%A8lzbench%E8%BF%9B%E8%A1%8C%E6%80%A7%E8%83%BD%E6%B5%8B%E8%AF%95)
的块大小、时长和数据集，只将编解码器选项换为 LZ4：

```bash
./lzbench -elz4 -b4 -t20u20 silesia.tar
```

`-b4` 指定 4 KiB 数据块；`-t20u20` 分别给压缩和解压约 20 秒测量时间。
LZ4 不启动后台服务。可在仓库根目录独立运行完整流程：

```bash
bash software/HPC/lz4/lz4_test.sh \
  --version 1.9.4 \
  --results-dir /home/runner/boostkit-perf/lz4/results/1.9.4
```

## 指标与输出

`benchmark_lzbench.txt` 保存完整原始输出，`benchmark_lz4.json` 保存解析后的
数值、实际命令和数据校验值。报告只列出这一条命令的三项结果：

| 指标 | lzbench 原始列 | 单位 | 优化方向 |
|---|---|---|---|
| 压缩吞吐 | `Compress.` | MB/s | 越大越好 |
| 解压吞吐 | `Decompress.` | MB/s | 越大越好 |
| 压缩后大小比例 | `Ratio` | % | 越小越好 |

只有完整的 LZ4 1.9.4 结果且运行参数一致时才生成结构化指标。独立运行
另会生成环境、构建、状态和报告文件；Framework 负责跨架构结果汇总。
