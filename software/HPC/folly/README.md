# Folly 性能测试

版本 `2026.08.17.00`。脚本入口为 `folly_test.sh`，Framework 阶段函数见 `case.yaml`。

## 构建

脚本从 [Folly 官方仓库](https://github.com/facebook/folly)浅克隆 `v2026.08.17.00`，在本次任务的 `PERF_WORK_DIR` 下构建。缺少命令或开发头文件时用 `dnf` 安装；`PERF_PROXY` 非空时作为 dnf 代理。构建还会取得脚本声明的 fast_float 源码版本。

主要配置和构建命令为：

```bash
cmake -S folly-source -B folly-build -DBUILD_BENCHMARKS=ON
cmake --build folly-build -j"$(nproc)" --target <脚本选出的官方 benchmark targets>
```

实际配置还包含脚本设置的 GoogleTest 源码发现选项；目标清单由源码中的官方 `BENCHMARK` 声明生成，记录在隔离工作区的 `benchmark_manifest.json`。不安装到系统目录。

## 测试与指标

`test` 阶段逐个运行所选官方 benchmark 可执行文件。一般目标使用 `--bm_json_verbose=<输出文件>`；两个不支持该参数的目标使用其官方文本输出。每个目标的 stdout、JSON 或文本均保留，`scripts/parse_benchmark.py` 将可比测量写入 `RESULTS_DIR/benchmark_folly.json`。

报告的动态指标名保留官方测试名称；数值、单位和方向来自解析结果，不对不同操作求一个总分。目标选择与过滤规则以 `folly_test.sh` 为准。

独立运行：

```bash
bash software/HPC/folly/folly_test.sh --version 2026.08.17.00 --results-dir /home/runner/folly-results
```
