# PyTorch 性能测试说明

本目录测试 PyTorch 官方 CPU 预编译 wheel，并使用 PyTorch 官方基准 API
`torch.utils.benchmark` 执行核心 CPU 算子基准测试，进行 x86_64 与 aarch64
开箱性能对比。软件入口为 `pytorch_test.sh`，Framework 通过 `case.yaml`
调用其 `build`、`start`、`test`、`stop` 四个阶段；直接执行该脚本也使用
同一套阶段函数。

当前清单仅声明 PyTorch `2.13.0`。新增版本时，必须同步更新 `case.yaml` 的
版本与 wheel 索引声明，以及 `pytorch_test.sh` 中的 Python 版本支持矩阵，
不能跳过版本校验。

## 构建与安装

这里的“构建”是部署官方预编译 wheel，不从 `https://github.com/pytorch/pytorch`
源码仓库编译，也不安装系统级 torch RPM。

1. 确认系统 `python3` 版本在官方 CPU wheel 支持矩阵内（`3.10`–`3.15`）。
2. 在本次任务工作目录 `PERF_WORK_DIR/torch-venv` 中创建任务私有虚拟环境
   （框架会为每个 case 预建 `PERF_WORK_DIR/venv`，该路径被框架保留）。
3. 从 PyTorch 官方 CPU wheel 索引安装精确版本，依赖包来自华为云 PyPI
   镜像（国产 Runner 无法稳定访问 `files.pythonhosted.org`）：

```bash
python3 -m venv "${PERF_WORK_DIR}/torch-venv"
"${PERF_WORK_DIR}/torch-venv/bin/pip" install --no-cache-dir \
  --index-url https://download.pytorch.org/whl/cpu \
  --extra-index-url https://mirrors.huaweicloud.com/repository/pypi/simple \
  --trusted-host mirrors.huaweicloud.com \
  "torch==2.13.0+cpu"
```

4. 导入部署的 torch 并校验 `torch.__version__` 必须精确等于
   `2.13.0+cpu`，校验失败立即退出；实际版本写入
   `results/<版本>/<运行 ID>/actual-version.txt`。
5. 运行时缺失的系统依赖（`python3`、`python3-pip`、`curl` 等）由脚本自动
   通过 `dnf` 安装；非 root Runner 使用 `sudo -n dnf`。

虚拟环境仅供本次任务使用，不写入系统 Python 环境。

## 运行时启动

`start` 阶段对部署的 torch 运行冒烟算子：固定随机种子生成 256×256 矩阵并
执行矩阵乘法，校验结果全部为有限值。冒烟通过后才进入测试阶段；不会复用
预先存在的系统级 torch。

```bash
"${PERF_WORK_DIR}/torch-venv/bin/python" - <<'PY'
import torch
torch.manual_seed(0)
b = torch.randn(256, 256) @ torch.randn(256, 256)
assert torch.isfinite(b).all()
PY
```

## 性能测试

测试采用 PyTorch 官方基准 API `torch.utils.benchmark.Timer` 的
`blocked_autorange` 自适应计时，保留每次运行的中位数（median）：

| 算子 | 分组 | 负载 | 派生吞吐 |
|---|---|---|---|
| `matmul_square_512` | matmul | 512×512 方阵乘法 | GFLOP/s |
| `matmul_square_1024` | matmul | 1024×1024 方阵乘法 | GFLOP/s |
| `matmul_square_2048` | matmul | 2048×2048 方阵乘法 | GFLOP/s |
| `conv2d_3x3_64x56` | conv | 1×64×56×56 输入，64×64×3×3 卷积核 | GFLOP/s |
| `elementwise_add_32m` | elementwise | 两个 8M 元素 float32 张量相加 | GB/s |
| `reduction_sum_64m` | reduction | 16M 元素 float32 求和 | GB/s |

每个算子按 `1`、`4`、`16` 线程档位运行（`TORCH_THREAD_LEVELS` 可覆盖）；
线程数通过 `torch.set_num_threads` 设置，inter-op 线程固定为 1 以隔离
框架内部并行。不绑定 CPU，由 Runner 按其正常调度策略执行。任何测量失败、
指标为空/非正/非有限，都会使测试失败。

可以脱离 Workflow 执行完整流程；脚本会依次执行 build、start、test、stop
并保存同样的产物。例如：

```bash
bash software/AI/pytorch/pytorch_test.sh \
  --version 2.13.0 \
  --results-dir /home/runner/boostkit-perf/pytorch/results/2.13.0
```

## 指标

每个“算子 + 线程数”组合保留中位耗时，声明了工作量的算子额外保留派生
吞吐量，不做平均、加权、评分或跨算子聚合。因此总计为：

```text
6 个算子 × 3 个线程档位 × 1 个耗时指标 = 18 个耗时指标
4 个派生吞吐算子 × 3 个线程档位 × 1 个吞吐指标 = 18 个吞吐指标
共 36 个指标
```

| 指标 | 报告中的指标名格式 | 单位 | 优化方向 | 含义 |
|---|---|---|---|---|
| `median_time_ms` | `torch <算子> --threads=<线程数>: median_time_ms` | ms | 越小越好 | blocked_autorange 中位耗时 |
| `GFLOP/s` | `torch <算子> --threads=<线程数>: GFLOP/s` | GFLOP/s | 越大越好 | 按声明 FLOPs 派生的计算吞吐 |
| `GB/s` | `torch <算子> --threads=<线程数>: GB/s` | GB/s | 越大越好 | 按声明字节数派生的访存吞吐 |

报告按算子分组展示，便于在相同负载下比较 x86_64 与 aarch64。测试工具及
其固定版本会同时列在报告的“测试环境”部分。

## 结果与清理

`case.yaml` 要求以下测试产物：

- `benchmark_torch_raw.log`：完整控制台输出（含每次测量的原始中位数）；
- `results.json`：36 个结构化指标及其分组与来源字段。

`stop` 阶段删除本次任务创建的虚拟环境目录。Framework 随后执行 Runner 级
环境清理。
