# Zstd 1.5.7 测试说明

本用例在 x86_64 和 aarch64 上使用同一份 Silesia 数据、同一组 lzbench 参数，
测试 Zstd 1.5.7 的 5 级压缩和解压性能。Framework 依次调用
`zstd_test.sh` 的 `build`、`start`、`test`、`stop` 函数。

## 构建和安装

脚本缺少依赖时通过 `dnf` 安装；非 root 用户使用 `sudo -n dnf`。源码和构建
结果只存于本次运行的 `/home/runner/boostkit-perf/zstd/` 子目录，不安装到系统路径。

```bash
git clone --branch v1.5.7 --depth 1 https://github.com/facebook/zstd.git zstd-source
git clone --branch v2.2 --depth 1 https://github.com/inikep/lzbench.git lzbench-source
diff -qr zstd-source/lib lzbench-source/lz/zstd/lib
make -C lzbench-source -j4 BUILD_STATIC=0 DONT_BUILD_DENSITY=1
```

lzbench v2.2 自带 Zstd 1.5.7 源码。构建前必须确认其 `lib` 目录与官方
v1.5.7 源码逐文件一致；随后 lzbench 编译这份源码。`DONT_BUILD_DENSITY=1`
只跳过与本测试无关的 Rust Density 编解码器。

## 数据与测试

脚本优先读取 `/home/runner/software/zstd/silesia.tar`。若不存在，则从
`https://wanos.co/assets/silesia.tar` 下载到本次私有工作目录。测试前核验
SHA-256：`ea122ed051dc7a6c58d2bb56bb05b34d9f1537c4dc9e71519142e2ca8cd6338d`。

测试命令沿用 [lzbench 使用示例](https://atomgit.com/boostkit/snappy/blob/master/docs/zh/quick_start.md#%E4%BD%BF%E7%94%A8%E7%A4%BA%E4%BE%8B%E4%BD%BF%E7%94%A8lzbench%E8%BF%9B%E8%A1%8C%E6%80%A7%E8%83%BD%E6%B5%8B%E8%AF%95)
的块大小、时长和数据集，只将编解码器选项换为 Zstd 5 级：

```bash
./lzbench -ezstd,5 -b4 -t20u20 silesia.tar
```

`-b4` 指定 4 KiB 数据块；`-t20u20` 分别给压缩和解压约 20 秒测量时间。
Zstd 不启动后台服务。可在仓库根目录独立运行完整流程：

```bash
bash software/HPC/zstd/zstd_test.sh \
  --version 1.5.7 \
  --results-dir /home/runner/boostkit-perf/zstd/results/1.5.7
```

## 指标与输出

`benchmark_lzbench.txt` 保存完整原始输出，`benchmark_zstd.json` 保存解析后的
数值、实际命令和数据校验值。报告只列出这一条命令的三项结果：

| 指标 | lzbench 原始列 | 单位 | 优化方向 |
|---|---|---|---|
| 压缩吞吐 | `Compress.` | MB/s | 越大越好 |
| 解压吞吐 | `Decompress.` | MB/s | 越大越好 |
| 压缩后大小比例 | `Ratio` | % | 越小越好 |

只有完整的 Zstd 1.5.7 5 级结果且运行参数一致时才生成结构化指标。独立运行
另会生成环境、构建、状态和报告文件；Framework 负责跨架构结果汇总。
