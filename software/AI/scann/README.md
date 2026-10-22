# ScaNN 性能测试

版本 `1.4.0`。脚本入口为 `scann_test.sh`；阶段函数和产物定义见 `case.yaml`。

## 安装

缺少 Python 或 pip 时，脚本用 `dnf` 安装相应 RPM；`PERF_PROXY` 非空时用于 dnf 的 `proxy` 选项。软件本体使用官方预编译 Python 包，安装到本次任务的 `PERF_WORK_DIR/scann-install`，不覆盖系统 Python：

```bash
python3 -m pip install --disable-pip-version-check --no-input --no-cache-dir --only-binary=:all: --target scann-install 'scann==1.4.0'
```

脚本按系统选择 pip 索引，随后导入 `scann`、`numpy` 并读取已安装包版本，要求与请求版本一致。

## 测试与指标

`test` 阶段运行 `python3 scripts/benchmark.py`。脚本用固定随机种子生成向量，对 `dot_product` 和 `squared_l2` 两种距离分别构建索引和查询，并与精确搜索结果计算 recall@K。数据规模、维度、重复次数和 K 由主脚本的 `DATA_SCALE`、`DATA_DIM`、`ITERATIONS`、`K` 提供。

`RESULTS_DIR/benchmark.json` 保存测试参数与原始测量。报告分别显示两种距离的建索引时间（s，越低越好）、QPS（queries/s，越高越好）、单查询延迟（µs，越低越好）和 recall@K（比例，越高越好）。

独立运行：

```bash
bash software/AI/scann/scann_test.sh --version 1.4.0 --results-dir /home/runner/scann-results
```
