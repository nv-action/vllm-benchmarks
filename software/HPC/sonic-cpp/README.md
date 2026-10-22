# sonic-cpp 性能测试

源码固定为上游提交 `69deb02c099361139dd387eb6c7034a4836ce9b3`（简称 `69deb02`）。构建和测试参考 [BoostKit 快速入门](https://atomgit.com/boostkit/sonic-cpp/blob/master/docs/zh/quick_start.md)，使用 sonic-cpp 自带的 `benchmark/bench`。入口为 `sonic_cpp_test.sh`；Framework 阶段及产物声明见 `case.yaml`。

## 构建

脚本将源码克隆到本次任务的私有 `PERF_WORK_DIR/sonic-cpp-source`，检出并核验 `69deb02`，然后构建 benchmark；不会安装或修改系统中的 sonic-cpp。缺少基础构建命令时使用 `dnf` 安装。

```bash
git clone https://github.com/bytedance/sonic-cpp.git sonic-cpp-source
git -C sonic-cpp-source checkout --detach 69deb02
cmake -S sonic-cpp-source -B build -DBUILD_BENCH=ON
cmake --build build --target bench -j
```

产物是 `build/benchmark/bench`。以上路径由脚本放在本次任务的私有工作目录下。x86_64 和 aarch64 均使用同一提交、同一构建选项；不应用鲲鹏 950 专用补丁，不启用 `ENABLE_SVE2_256`。

## 测试与指标

`test` 阶段在源码目录运行参考文档的 Sonic 场景，最短测量时间为每项 3 秒，不绑核。脚本额外要求 Google Benchmark 写出 JSON，供 Framework 收集和校验：

```bash
build/benchmark/bench \
  --benchmark_filter=Sonic \
  --benchmark_min_time=3s \
  --benchmark_out_format=json \
  --benchmark_out=<结果目录>/benchmark.json
```

`scripts/parse_benchmark.py` 将 JSON 中每项 Sonic 测量的 `cpu_time` 统一换算为 ns，保留原始场景名称、数值和单位，写入 `benchmark_sonic_cpp.json`。报告逐项比较耗时（越低越好），不计算合成总分。若没有 Sonic 测量或出现重复场景，测试失败。两个 JSON 都是必需产物。

独立运行：

```bash
bash software/HPC/sonic-cpp/sonic_cpp_test.sh \
  --version 69deb02 \
  --results-dir /home/runner/sonic-cpp-results
```
