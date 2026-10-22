# hnswlib 性能测试

被测软件：[nmslib/hnswlib](https://github.com/nmslib/hnswlib)，固定版本 `0.8.0`。

测试框架地址：[sra_test](https://atomgit.com/liuliuyiyidingding/sra_test)，固定提交 `9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf`。脚本入口是 `hnswlib_test.sh`，Workflow 阶段定义见 `case.yaml`。

## 构建

脚本从 hnswlib 官方仓库克隆请求版本的精确 tag，并校验 `hnswlib/hnswlib.h`。hnswlib 是头文件库；无需安装 Python 包，也不修改系统目录。

随后将固定提交的 `sra_test` 稀疏检出到本次任务的 `PERF_WORK_DIR/sra-test`，把 hnswlib 头文件路径写入私有的 `build/config_hnswlib.sh`，执行原始构建命令：

```bash
make hnswlib_test
```

缺少 `git`、`g++`、`make`、`curl`、`h5dump` 或 HDF5 开发头文件时，脚本使用现有系统仓库的 `dnf` 安装对应依赖，不添加软件仓库。源码、测试程序和生成的索引均留在本次任务的私有工作目录中。

## 数据与测试

测试使用以下五个数据集，依次运行：

- `sift-128-euclidean`
- `glove-100-angular`
- `deep-image-96-angular`
- `fashion-mnist-784-euclidean`
- `gist-960-euclidean`

每个数据集放在 `/home/runner/software/hnswlib/data/<数据集名称>.hdf5`。脚本优先读取本地文件；缺失时从 `https://ann-benchmarks.com/<数据集名称>.hdf5` 下载到同一目录。运行前检查 HDF5 必需字段，并记录 SHA-256 校验值。

默认只运行原始的 `hnswlib_test`：同一个程序分别处理五个数据集，共 5 次测试。例如其中一次执行：

```bash
./hnswlib_test hnswlib sift-128-euclidean --no-pin
```

`--no-pin` 表示不做 NUMA 绑核。测试参数直接读取 `sra_test/configs/hnswlib/` 下每个数据集的原始配置。每次在私有工作目录中新建索引，不加载预建索引，也不执行参数寻优。FP16 测试程序未纳入跨架构默认矩阵。

每次测试的原始输出保存在 `RESULTS_DIR/sra-test-logs/`。`RESULTS_DIR/benchmark_sra.json` 保存构建时间、Recall、墙钟 QPS，以及仅供追溯的平均线程耗时推算 QPS；报告按数据集分组。

## 独立运行

```bash
bash software/AI/hnswlib/hnswlib_test.sh --version 0.8.0 \
  --results-dir /home/runner/hnswlib-results
```

调试时加 `--keep-workdir` 保留私有构建目录。
