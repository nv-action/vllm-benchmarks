# glibc 2.31 性能测试

本用例从 [GNU glibc 2.31 官方发布包](https://ftp.gnu.org/gnu/glibc/glibc-2.31.tar.xz)构建 glibc，在任务私有目录安装并测试。只执行 `memcpy`、`memcmp` 两个官方 benchtest 程序，不替换系统 glibc，不使用 `LD_PRELOAD`，也不绑核。

## 源码、构建与安装

脚本优先读取 `/home/runner/software/glibc/glibc-2.31.tar.xz`；不存在时从 GNU 官方地址下载。两种来源均校验官方发布包的 SHA-256：

```text
9246fe44f68feeec8c666bb87973d590ce0137cca145df014c72ec95be9ffd17
```

glibc 2.31 使用系统 GNU Make 4.4.1 时可能在 `stdio-common/others` 反复重执行。脚本优先读取 `/home/runner/software/glibc/make-4.3.tar.gz`，没有则从 [GNU 官方目录](https://ftp.gnu.org/gnu/make/)下载；校验 SHA-256 `e05fdde47c5f7ca45cb697e973894ff4f5d79e13b750ed57d7b66d8defc78e19` 后，将 GNU Make 4.3 构建到本次 `PERF_WORK_DIR/make-4.3-install`。glibc 的构建、安装和 benchtest 阶段均优先使用这个私有 `make`，不替换系统命令。参见 [glibc 官方问题记录](https://sourceware.org/pipermail/glibc-bugs/2024-February/056404.html)。

缺少构建命令时，脚本通过 `dnf` 安装对应系统包。glibc 源码、构建目录和安装前缀分别为本次 `PERF_WORK_DIR` 下的 `glibc-src`、`glibc-build`、`glibc-install`。主要命令是：

```bash
cd "$PERF_WORK_DIR/glibc-build"
export PATH="$PERF_WORK_DIR/make-4.3-install/bin:$PATH"
../glibc-src/configure \
  --prefix="$PERF_WORK_DIR/glibc-install" \
  --disable-werror \
  --without-selinux \
  CC=gcc CFLAGS=-O3
make -j"$(nproc)" CXX=
make install cross-compiling=yes CXX=
```

`CXX=` 让 glibc 2.31 使用其自带的 C 版构建辅助程序，避免链接 Runner 上需要更新 glibc 符号的 `libstdc++`。`--without-selinux` 在两架构上统一关闭可选 SELinux 集成，避免构建辅助程序链接依赖较新 glibc 的系统 `libselinux`；被测 `memcpy`、`memcmp` 的实现不变。安装命令不会更新 Runner 的系统动态链接器缓存。脚本用本次构建的 `elf/ldconfig --version` 和隔离前缀中的动态加载器校验结果。指导文档中的 `--enable-benchtests` 不属于 glibc 2.31 的配置选项。

## 计时与测试

glibc 2.31 原始 benchtests 在 x86_64 使用 TSC 计数，在 aarch64 使用纳秒。两架构均对相同的官方源码应用本目录的 [`scripts/glibc-2.31-benchtests.patch`](scripts/glibc-2.31-benchtests.patch)：

- `bench-timing.h`：统一用 `clock_gettime(CLOCK_MONOTONIC)`，输出单位为 ns。
- `bench-string.h`：保留实际公开 `memcpy`、`memcmp` 函数入口，避免仅记录内部实现。
- `bench-memcmp.c`：在原始输出中标记相等与不相等输入，以便准确选择同一场景。

补丁仅修改基准程序，不修改被测 glibc 函数实现。两架构都必须输出 `clock_gettime_ns` 计时标记，否则测试失败。

```bash
make -C "$PERF_WORK_DIR/glibc-src/benchtests" \
  objdir="$PERF_WORK_DIR/glibc-build" \
  "$PERF_WORK_DIR/glibc-build/benchtests/bench-timing-type" \
  "$PERF_WORK_DIR/glibc-build/benchtests/bench-memcpy" \
  "$PERF_WORK_DIR/glibc-build/benchtests/bench-memcmp" CXX=
cd "$PERF_WORK_DIR/glibc-build"
./benchtests/bench-memcpy > /path/to/results/benchtests/bench-memcpy.out
./benchtests/bench-memcmp > /path/to/results/benchtests/bench-memcmp.out
```

只构建、运行上述两个官方基准程序，不生成无关字符串基准或 locale。性能结果选取两者共同具备的固定场景：长度 2048、4096 字节，两个地址都按 0 偏移对齐；`memcmp` 使用相等输入。报告按 `memcpy`、`memcmp` 分组，各有两项 ns/调用指标，越小越好。原始 JSON 包含官方程序输出的其余输入场景和实现，保留在 `benchtests/`。

直接执行完整流程：

```bash
bash software/Toolchain/glibc/glibc_test.sh \
  --version 2.31 \
  --results-dir /home/runner/boostkit-perf/glibc/results/2.31
```

结果目录还包含 `benchmark_bench.txt`、`benchmark_glibc.json` 和阶段信息；Framework 从 `benchmark_glibc.json` 读取与两架构同名的四项指标，生成跨架构报告。
