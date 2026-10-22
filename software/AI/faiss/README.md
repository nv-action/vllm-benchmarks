# Faiss 1.14.3 性能测试

测试框架地址：[sra_test](https://atomgit.com/liuliuyiyidingding/sra_test)。本适配使用其 C++ 测试程序，固定提交 `9a941bc3fb72c1e0d8dc48e6deb7df33e3e23abf`。入口是 `faiss_test.sh`，Workflow 阶段定义见 `case.yaml`。

## 构建

脚本从 Faiss 官方仓库克隆 `v1.14.3`，在本次任务的 `PERF_WORK_DIR` 中构建 CPU-only Release 共享库：

```bash
git clone --branch v1.14.3 --depth 1 https://github.com/facebookresearch/faiss.git faiss-source
cmake -B faiss-build -S faiss-source \
  -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_PYTHON=OFF \
  -DFAISS_ENABLE_EXTRAS=OFF \
  -DBUILD_TESTING=OFF -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release
make -C faiss-build -j"$(nproc)" faiss
```

随后稀疏检出固定提交的 `sra_test`（不获取仓库内的预建索引），把刚构建的 Faiss 库、头文件路径写入其私有 `build/config_faiss_<架构>.sh`。默认只编译原仓库的 HNSW 目标：

```bash
make hnsw_test
```

需要全量测试时，设置 `FAISS_BENCH_PROFILE=all`，构建阶段才会再编译其余六个目标：

```bash
make ivfpq_test
make ivfpqfs_test
make pqfs_test
make ivfflat_test
make ivfrabitq_test
make ivfrabitqfs_test
```

脚本用现有系统仓库的 `dnf` 安装缺少的通用构建包；要求 CMake ≥ 3.24、支持 C++20 和 OpenMP 的 `g++`。不会添加软件仓库。

- `x86_64`：使用 Intel oneMKL。先检查 `MKLROOT` 和已有安装；若缺失，优先读取 `/home/runner/software/faiss/intel-onemkl-2026.1.0.237_offline.sh`，否则从 Intel 官方地址下载相同的完整离线安装包。以普通用户静默安装到本次任务的 `PERF_WORK_DIR/oneapi`，并使用独立安装实例避免与机器上同版本的安装冲突；随后检查 `mkl.h`、`libmkl_intel_lp64.so`、`libmkl_gnu_thread.so`、`libmkl_core.so`。不通过 `dnf` 安装 Intel RPM。
- `aarch64`：使用系统 OpenBLAS；缺少时从现有系统仓库安装 `openblas-devel`，并通过 `pkg-config` 读取编译和链接参数。

构建前会实际编译、链接并运行数学库探针；配置 Faiss 后检查其链接命令是否使用该架构指定的数学库。Faiss 和测试程序均不安装到系统目录。x86_64 的私有 oneMKL 安装随本次任务的工作目录一起清理。

## 数据与测试

测试使用以下五个数据集，依次运行，不需要手动逐项启动：

- `sift-128-euclidean`
- `glove-100-angular`
- `deep-image-96-angular`
- `fashion-mnist-784-euclidean`
- `gist-960-euclidean`

每个数据集的文件放在 `/home/runner/software/faiss/data/<数据集名称>.hdf5`。脚本优先读取本地文件；文件不存在时，从 `https://ann-benchmarks.com/<数据集名称>.hdf5` 下载到同一目录。运行前会检查文件内容，并记录 SHA-256 校验值。

默认只测试 HNSW：同一个 `hnsw_test` 程序分别处理上述五个数据集，共 5 次测试。例如其中一次实际执行：

```bash
./hnsw_test hnsw sift-128-euclidean --no-pin
```

`--no-pin` 表示不做 NUMA 绑核。测试参数沿用 `sra_test` 的配置文件；每次在私有工作目录中新建索引，不使用预建索引，也不执行参数寻优。

需要运行全部 7 种算法时，设置 `FAISS_BENCH_PROFILE=all`：每种算法都测试这五个数据集，共 35 次测试。该选项必须在构建、启动和测试阶段保持一致，否则测试程序可能尚未编译。独立运行时也可使用下文的 `--profile all`。

每次测试的原始输出保存在 `RESULTS_DIR/sra-test-logs/`。`RESULTS_DIR/benchmark_sra.json` 汇集构建时间、Recall 和 QPS 等指标，报告按数据集分组。

## 独立运行

```bash
bash software/AI/faiss/faiss_test.sh --version 1.14.3 \
  --results-dir /home/runner/faiss-results
```

调试时加 `--keep-workdir` 保留私有构建目录。
