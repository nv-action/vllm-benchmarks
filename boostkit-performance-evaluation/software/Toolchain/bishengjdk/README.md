# 毕昇 JDK 21 性能测试

本用例从毕昇 JDK 21.0.9 GA 源码构建 JDK，性能测试运行该源码自带的
`FloatingScalarVectorAbsDiff` JMH 微基准，报告四个方法的平均耗时
（`ns/op`，越低越好）。测试不绑核或绑定 NUMA 内存。

`case.yaml` 依次调用 `bishengjdk_test.sh` 的 `build`、`start`、`test`、`stop`
阶段；独立运行使用同一套构建和测试函数。

## 文件、来源和目录

脚本优先读取 `/home/runner/software/bishengjdk/` 的离线包；没有时下载。
所有解包、构建和测试文件位于本次任务的私有 `${PERF_WORK_DIR}`，不会安装到系统 JDK。

| 用途 | 离线文件 | 网络来源 | 本次任务内的位置 |
|---|---|---|---|
| 毕昇 JDK 源码 | `bishengjdk-21-jdk-21.0.9-ga-b011.tar.gz` | `https://github.com/openeuler-mirror/bishengjdk-21/archive/refs/tags/jdk-21.0.9-ga-b011.tar.gz` | `bishengjdk-source/` |
| Boot JDK，x86_64 | `OpenJDK21U-jdk_x64_linux_hotspot_21.0.9_10.tar.gz` | `https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.9%2B10/` | `boot-jdk/` |
| Boot JDK，aarch64 | `OpenJDK21U-jdk_aarch64_linux_hotspot_21.0.9_10.tar.gz` | 同上 | `boot-jdk/` |
| JMH 1.37 及依赖（共四个 JAR） | 无单独离线包 | `make/devkit/createJMHBundle.sh` 从 Maven 仓库下载 | `bishengjdk-source/build/jmh/jars/` |

源码的主仓库为 [openEuler/bishengjdk-21](https://gitee.com/openeuler/bishengjdk-21)，
下载地址是它的 GitHub 镜像，固定标签为 `jdk-21.0.9-ga-b011`。
Boot JDK 只用于编译，测试使用本次源码构建的 JDK。
如已有解压后的 Boot JDK，可通过 `BISHENGJDK_BOOT_JDK_HOME` 指向其根目录；
脚本检查 `java`、`javac` 和版本 `21.0.9`。

源码和 Boot JDK 下载后分别记录 SHA-256 或校验实际版本。
JMH 下载脚本的 Maven 地址可用 `BISHENGJDK_MAVEN_MIRROR` 指定；默认是
`https://repo.maven.apache.org/maven2`。四个 JAR 必须全部存在且非空。

## 构建

脚本先通过 `dnf` 补齐缺少的 JDK 构建依赖，包括 ALSA、CUPS、字体和 X11
开发头文件；不使用第三方 RPM 仓库。然后在源码根目录执行：

```bash
cd "${PERF_WORK_DIR}/bishengjdk-source"
sh make/devkit/createJMHBundle.sh
bash configure \
  --with-boot-jdk="${PERF_WORK_DIR}/boot-jdk" \
  --with-jmh=build/jmh/jars \
  --with-debug-level=release \
  --with-jvm-variants=server \
  --prefix="${PERF_WORK_DIR}/jdk" \
  --disable-warnings-as-errors \
  --disable-precompiled-headers
make images
```

构建配置为 `build/linux-x86_64-server-release/` 或
`build/linux-aarch64-server-release/`。生成的 `images/jdk` 保留在构建树内，
`${PERF_WORK_DIR}/jdk` 是指向它的链接，因此后续 `make test` 仍能找到 JDK image。
脚本通过 `jdk/bin/java -version` 验证实际源码构建版本为 `21.0.9-internal`。

## 测试

源码中必须存在
`test/micro/org/openjdk/bench/vm/compiler/FloatingScalarVectorAbsDiff.java`。
脚本在源码根目录执行以下命令；`CONF` 按实际架构取对应的构建配置名：

```bash
cd "${PERF_WORK_DIR}/bishengjdk-source"
make test CONF="${CONF}" \
  TEST="micro:org.openjdk.bench.vm.compiler.FloatingScalarVectorAbsDiff" \
  MICRO="FORK=3;WARMUP_ITER=4;WARMUP_TIME=2;ITER=4;TIME=2;RESULTS_FORMAT=json;OPTIONS=-t 1 -p count=1024"
```

每个方法使用 1 个 JMH 工作线程、3 个 fork；每个 fork 预热 4 次、测量 4 次，
每次 2 秒。四个方法为：

| 方法 | 数据类型 | 计算形式 |
|---|---|---|
| `testVectorAbsDiffFloat` | float | 数组循环 |
| `testVectorAbsDiffDouble` | double | 数组循环 |
| `testScalarAbsDiffFloat` | float | 标量依赖链 |
| `testScalarAbsDiffDouble` | double | 标量依赖链 |

脚本不执行 `numactl`，不设置 `CPU_SET` 或 `NUMA_NODE`。两种架构使用相同的
JMH 参数和完整方法名对齐，但 CPU 调度由各自系统决定。

## 指标和输出

从 JMH 原始 JSON 中逐项读取 `primaryMetric.score`。四项结果必须齐全，
且均为 `AverageTime`、`ns/op`、`count=1024`，否则测试失败。

| 输出 | 内容 |
|---|---|
| `jmh-output.log` | `make test` 的原始控制台输出。 |
| `jmh-result.json` | JMH 原始 JSON，保留 Score、误差和运行参数。 |
| `benchmark_bishengjdk.json` | 四项规范化指标及实际测试命令。 |

## 独立执行

```bash
bash software/Toolchain/bishengjdk/bishengjdk_test.sh --version 21.0.9
```

可用 `--results-dir <目录>` 指定持久结果目录，或用 `--keep-workdir` 保留构建树。
