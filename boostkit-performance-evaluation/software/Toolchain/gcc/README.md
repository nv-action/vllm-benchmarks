# GCC 14.4.0 性能测试

本用例在 `x86_64` 和 `aarch64` Runner 上分别从源码构建 GCC 14.4.0，再用构建出的
GCC 运行 SPEC CPU2017 1.0.5 的整数吞吐量套件 `intrate`。两侧使用各自的完整 SPEC
配置文件，执行相同的 384 副本、1 次迭代测试。

入口是 `gcc_test.sh`。Framework 根据 `case.yaml` 依次调用 `build`、`start`、
`test`、`stop` 阶段；也可直接运行脚本完成同一流程。

## 运行前准备

| 文件 | 放置位置 | 获取方式 |
|---|---|---|
| GCC 源码包 | `/home/runner/software/gcc/gcc-14.4.0.tar.xz` | 可选；缺失时下载 `https://gcc.gnu.org/pub/gcc/releases/gcc-14.4.0/gcc-14.4.0.tar.xz` |
| SPEC CPU2017 ISO | `/home/runner/software/gcc/cpu2017-1.0.5.iso` | 必需；由使用者预置，脚本不下载受许可的 ISO |

`runner` 用户必须能读取 ISO，并能无密码执行 `sudo`：流程中需要挂载、卸载 ISO
以及修改内核参数。缺失的 GCC 构建依赖和 `numactl` 由脚本通过 `dnf`
安装；SPEC 自带工具所需的 `libnsl.so.1` 也会检查。下载或使用离线 GCC 包后，
脚本都会校验其 SHA-256。

所有构建和测试文件位于本次任务的 `${PERF_WORK_DIR}`，不替换系统 GCC。
独立运行时，工作目录默认位于 `/home/runner/boostkit-perf/gcc/local-<运行ID>/`：

| 子目录 | 用途 |
|---|---|
| `gcc-src/` | GCC 源码 |
| `gcc-build/` | GCC 独立构建目录 |
| `gcc-install/` | 本次任务使用的 GCC 安装目录 |
| `data/gcc-<运行ID>/cpu2017-media/` | ISO 只读挂载点 |
| `data/gcc-<运行ID>/cpu2017/` | 本次任务的 SPEC 安装及运行目录 |

## 构建 GCC

脚本在 `gcc-build/` 中执行以下命令，再检查 `gcc`、`g++`、`gfortran` 的可执行文件和
实际版本：

```bash
cd "${PERF_WORK_DIR}/gcc-build"
"${PERF_WORK_DIR}/gcc-src/configure" \
  --prefix="${PERF_WORK_DIR}/gcc-install" \
  --enable-languages=c,c++,fortran \
  --disable-bootstrap \
  --disable-multilib \
  --disable-nls
make -j"$(nproc)"
make install
```

## 安装 SPEC 与选择配置

测试阶段把 ISO 只读挂载到本次运行的 `data/gcc-<运行ID>/cpu2017-media/`，
然后从挂载目录执行 ISO 自带的安装器：

```bash
sudo -n mount -o loop,ro /home/runner/software/gcc/cpu2017-1.0.5.iso \
  "${PERF_WORK_DIR}/data/gcc-${PERF_RUN_ID}/cpu2017-media"
cd "${PERF_WORK_DIR}/data/gcc-${PERF_RUN_ID}/cpu2017-media"
env -u SPEC ./install.sh -f -d "${PERF_WORK_DIR}/data/gcc-${PERF_RUN_ID}/cpu2017"
```

这只安装到本次任务目录，不安装系统级 SPEC 软件包。安装后，脚本按架构选择仓库中的
完整配置，并以原文件名放入 SPEC 安装目录的 `config/`：

| 架构 | 配置文件 |
|---|---|
| `x86_64` | `scripts/spec-gcc-x86.cfg` |
| `aarch64` | `scripts/spec-gcc-aarch64.cfg` |

放置配置时只把其中的 `gcc_dir` 改为本次任务的 `gcc-install/` 路径。两份配置均使用
`14.4.0-base` 标签，保留原配置的 `-O3`、移植性选项、`output_root`、NUMA 绑核和
其他条件分支；x86 配置仅在必须的架构选项及 Perl 平台宏上不同。

## 运行测试

脚本先记录当前 ASLR 值，将 ASLR 设为 `0` 并清空页缓存。随后进入 SPEC 安装目录，
执行对应架构的命令：

```bash
source ./shrc

# x86_64
runcpu --config=spec-gcc-x86.cfg intrate -n 1 -C 384

# aarch64
runcpu --config=spec-gcc-aarch64.cfg intrate -n 1 -C 384
```

`-n 1` 表示运行 1 次；`-C 384` 将配置文件默认的 `copies=1` 覆盖为 384 个并行
副本。配置中的 `submit` 仍然生效：在当前未定义 `type`、`start_core=0` 的设置下，
副本按序绑定到 CPU 0–383，并通过 `numactl --localalloc` 使用本地 NUMA 内存。
Runner 必须允许使用这些 CPU 编号，否则测试会失败。

## 指标、结果与清理

报告只取 SPEC 官方 `intrate` 文本结果中的套件总分 `SPECrate2017_int_base`，
单位为比值，越高越好。`-n 1` 的结果是估算值，原文通常标为
`Est. SPECrate2017_int_base`；它不是可提交的 SPEC 正式成绩。

| 结果文件 | 内容 |
|---|---|
| `raw-output.log` | `runcpu` 控制台原始输出 |
| `spec-install.log` | SPEC 安装器原始输出 |
| `spec-results/` | SPEC 生成的原始结果文件 |
| `benchmark_gcc.json` | 从官方文本结果提取的总分、来源和实际命令 |
| `spec-gcc-x86.cfg` 或 `spec-gcc-aarch64.cfg` | 本次运行实际使用的配置 |
| `spec-build-logs/` | 测试构建失败时保留的 `make*.out` 日志 |

脚本从配置指定的 `output_root` 收集结果；对当前配置，SPEC 原始结果目录是
`${PERF_WORK_DIR}/data/gcc-<运行ID>/cpu2017/result/gnu/14.4.0-base/result/`。
结果复制到持久 `RESULTS_DIR` 后，`stop` 阶段恢复原 ASLR 值、卸载 ISO，
并仅清理带有本次运行标记的 `gcc-<运行ID>/`。Runner 上预置的 ISO、
数据根目录和持久结果目录不会删除。

## 独立运行

```bash
bash software/Toolchain/gcc/gcc_test.sh \
  --version 14.4.0 \
  --results-dir /home/runner/boostkit-perf/gcc/results/14.4.0
```

`--keep-workdir` 可保留 GCC 源码、构建和安装目录供排查；SPEC 数据目录仍由 `stop`
阶段清理。
