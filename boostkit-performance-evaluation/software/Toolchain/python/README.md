# Python 性能测试说明

本目录从 CPython 官方仓库构建指定版本解释器，并参考
[CinderX 的性能测试流程](https://gitcode.com/openeuler/cinderx/blob/master/cinderx/Docs/README_CN.md)，
直接运行官方 `pyperformance` 默认测试集，进行 `x86_64` 与 `aarch64` 的原生
CPython 开箱性能对比。CinderX 插件不参与测试。软件入口为
`python_test.sh`，Framework 通过 `case.yaml` 调用其 `build`、`start`、`test`
和 `stop` 四个阶段；直接执行该脚本也使用同一套阶段函数。

当前清单仅声明 CPython `3.14.7`。新增版本时，必须确认 CPython 官方标签存在，
并保持两种架构使用相同的 CPython 版本、构建选项和 pyperformance 版本。

## 构建与安装

这里的“构建”是从 CPython 官方源码构建私有解释器，不覆盖或升级 Runner 的系统
Python，也不向系统路径安装 Python。

1. 从 CPython 官方 Git 仓库浅克隆 `v<版本>` 标签；
2. 在本次任务的 `PERF_WORK_DIR/cpython-source` 中执行配置和编译；
3. 安装到本次任务私有的 `PERF_WORK_DIR/cpython-install`；
4. 校验私有解释器的实际版本、`pip` 与 `ssl`、`zlib`、`ctypes` 模块。

脚本自动检查并通过 `dnf` 安装缺失依赖；非 root Runner 使用 `sudo -n dnf`。其中包括
`git`、`gcc`、`make`、`python3`、`nproc` 以及 CPython 标准库模块和官方基准集所需
的 `openssl-devel`、`zlib-devel`、`libffi-devel`、`sqlite-devel`、`bzip2-devel`、
`xz-devel`、`readline-devel`、`gdbm-devel`、`uuid-devel`。

构建命令如下，安装前缀由每次任务运行时生成：

```bash
git clone --branch v3.14.7 --depth 1 https://github.com/python/cpython.git cpython-source
cd cpython-source
./configure \
  --prefix="${PERF_WORK_DIR}/cpython-install" \
  --enable-optimizations \
  --with-lto
make -j"$(nproc)"
make install
```

`--enable-optimizations` 启用 PGO，`--with-lto` 启用链接时优化。两项均是本用例
固定的性能构建配置，构建后的实际版本写入 `actual-version.txt`。

## 服务启动

Python 性能测试不启动后台服务。`start` 阶段仅检查本次任务构建出的私有解释器是否
可执行、是否能导入 `ssl`、`zlib` 和 `ctypes`，并确认 `pip` 可用；只有检查通过才
进入性能测试。`stop` 阶段没有待停止的进程。

核心检查命令如下：

```bash
"${PERF_WORK_DIR}/cpython-install/bin/python3" --version
"${PERF_WORK_DIR}/cpython-install/bin/python3" -c 'import ssl, zlib, ctypes'
"${PERF_WORK_DIR}/cpython-install/bin/python3" -m pip --version
```

## 性能测试

测试采用固定版本的 Python 官方性能测试套件：

| 组件 | 固定来源 |
|---|---|
| CPython | `https://github.com/python/cpython.git`，标签 `v3.14.7` |
| pyperformance | `1.13.0`，通过华为云 PyPI 安装 |

正式流程如下：

1. 使用本次任务私有 CPython 安装 `pyperformance==1.13.0`；
2. 执行文档中的默认测试命令 `pyperformance run --warmup 3`，不使用 `-b`
   限定三项代表性基准；
3. 由 pyperformance 运行其固定版本定义的默认测试集，每项预热 3 次；
4. 保留官方原始 JSON，并从每个 benchmark 的全部有效测量值中提取 median。

实际命令如下：

```bash
"${PYTHON_BIN}" -m pip install --no-cache-dir \
  --index-url https://mirrors.huaweicloud.com/repository/pypi/simple \
  --trusted-host mirrors.huaweicloud.com \
  pyperformance==1.13.0

"${PYTHON_BIN}" -m pyperformance run \
  --warmup 3 \
  --inherit-environ PIP_INDEX_URL,PIP_TRUSTED_HOST,http_proxy,https_proxy,no_proxy,all_proxy,HTTP_PROXY,HTTPS_PROXY,NO_PROXY,ALL_PROXY \
  -o "${RESULTS_DIR}/benchmark.json"
```

脚本将华为云 PyPI 地址与可信主机写入 `PIP_INDEX_URL`、`PIP_TRUSTED_HOST`，并通过
`--inherit-environ` 传入 pyperformance 创建的测试虚拟环境。文档示例的
`--affinity=300` 是单机 CPU 编号，这里不固定 CPU 编号；两种架构均由测试工具
选择可用 CPU。测试命令不设置 `CINDERX_PLUGIN_ENABLE` 或 `PYTHONJITAUTO`，因此
结果只代表原生 CPython。默认测试集耗时可能明显长于三项代表性基准，任务超时设置为
360 分钟。

可以脱离 Workflow 执行完整流程；脚本会依次执行 build、start、test、stop 并保存
相同的产物。例如：

```bash
bash software/Toolchain/python/python_test.sh \
  --version 3.14.7 \
  --results-dir /home/runner/boostkit-perf/python/results/3.14.7
```

## 指标

默认测试集中每个 pyperformance benchmark 保留一个指标，不做平均、加权、评分或
跨场景聚合。报告按原始 benchmark 名称对齐两个架构；具体项目和数量以
`pyperformance==1.13.0` 的实际输出为准。

| 原始字段 | 报告中的指标名 | 单位 | 优化方向 | 含义 |
|---|---|---:|---|---|
| 所有有效测量值 | `pyperformance <官方 benchmark 名称>: median` | s | 越小越好 | 该官方 benchmark 全部有效测量值的中位执行时间 |

解析时跳过仅含 warmup 的校准记录；其他每条测量值必须是正数且单位可换算为秒。官方
输出缺失、benchmark 重名、无有效测量值或无法换算单位时，测试立即失败。

## 结果与清理

`case.yaml` 要求以下测试产物：

- `benchmark.json`：pyperformance 生成的官方原始 JSON；
- `benchmark_python.json`：逐项 median 指标、执行命令、预热次数、构建参数及
  pyperformance 运行元数据。

`stop` 阶段不需要停止服务。直接执行入口脚本时，会在结果收集后删除本次自动创建的
工作目录；Framework 执行时，由 Runner 级环境清理移除该任务的隔离工作目录。
