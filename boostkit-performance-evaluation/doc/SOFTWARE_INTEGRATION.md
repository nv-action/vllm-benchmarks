# 软件测试接入指南

新软件放在 `software/<category>/<software>/`。脚本须支持 Framework 分阶段调用和项目外独立运行，两种入口共用构建、测试和解析代码。

文中的 `example`、`1.0.0`、下载地址、校验值和指标名均为示例。接入时须替换为核实过的真实值。

## 1. 确定测试并编写软件 README

先确定两个架构都能获取的版本和正式测试命令，再写 `software/<category>/<software>/README.md`：

| 项目 | README 中要写的内容 |
|---|---|
| 软件版本与来源 | 源码 tag、提交或二进制包地址；离线包文件名与校验值。 |
| 构建或安装 | 实际执行的命令、安装位置、读取产物版本的命令。 |
| 测试工具 | 来源、版本、准备命令和最终 benchmark 命令。 |
| 测试负载 | 数据集、请求或迭代数、并发、预热、预计内存、磁盘和运行时间。 |
| 结果解析 | 原始输出文件、选取的原始字段、单位和优化方向。 |

如果并发度随机器核数变化，记录实际并发度，并在指标身份中保留这一差异。跨架构报告只配对同一测试项。

## 2. 注册软件并填写 `case.yaml`

以 `HPC/example` 为例：

1. 在 `config/categories.yaml` 的 `HPC` 列表中追加 `- example`。
2. 创建 `software/HPC/example/README.md`、`case.yaml` 和 `example_test.sh`。
3. 将解析器等辅助程序放在 `software/HPC/example/scripts/`。
4. 在清单中填写 `category: HPC`、`name: example`，与注册名和目录名保持一致。

固定指标的清单可按下例填写，所有脚本和函数名都须实际存在：

```yaml
name: example
category: HPC
enabled: true
versions:
  - "1.0.0"
test_tools:
  example_benchmark:
    version: "1.0.0"
execution:
  type: shell-functions
  stages:
    build:
      script: example_test.sh
      function: build_example
    start:
      script: example_test.sh
      function: start_example
    test:
      script: example_test.sh
      function: run_example_benchmarks
    stop:
      script: example_test.sh
      function: stop_example
  timeout_minutes: 180
outputs:
  benchmark_raw:
    path: benchmark_raw.txt
    stage: test
    format: text
    required: true
  benchmark_result:
    path: benchmark.json
    stage: test
    format: json
    required: true
metrics:
  source: benchmark_result
  definitions:
    throughput:
      path: results.throughput
      unit: ops/s
      direction: higher_is_better
```

上例要求 `benchmark.json` 至少包含 `{"results":{"throughput":123.4}}`，且数值来自正式测试输出。填写清单时逐项核对：

- `outputs.path` 相对于 `RESULTS_DIR`；必需文件由对应阶段写出且非空，JSON 根节点为对象。
- `metrics.source` 指向必需的 JSON 输出；指标值为有限数值。
- `direction` 填 `higher_is_better`、`lower_is_better` 或 `neutral`。
- `test_tools` 填实际工具。版本固定时填确切版本；使用随软件构建的工具可填“与被测软件版本一致”；使用 `dnf` 或 `@latest` 动态安装的工具，应如实注明版本未固定及来源；来源也无法核实时填 `unknown`。

固定指标的实际案例：[Snappy 的 `case.yaml`](../software/HPC/snappy/case.yaml)。它用 `metrics.definitions` 将官方 benchmark 名称逐项映射到 JSON 数值路径。

测试项数量随工具输出变化时，将上例的 `metrics.definitions` 换成 `metrics.collection`。例如，对于 `{"results":{"case_a":{"value":123.4},"case_b":{"value":98.7}}}`：

```yaml
metrics:
  source: benchmark_result
  collection:
    path: results
    value_path: value
    unit: ops/s
    direction: higher_is_better
```

动态指标清单按输出结构填写：

- `results` 为非空对象；对象键 `case_a`、`case_b` 是指标名。
- 名称位于对象内部字段时，增加 `name_path`。
- 按场景分表时，增加 `group_path`；二维表格参照 [MySQL 的 `case.yaml`](../software/Database/mysql/case.yaml)。

动态指标的实际案例：[LZ4 的 `case.yaml`](../software/HPC/lz4/case.yaml)。它用 `metrics.collection` 提取测试项，并用 `group_path` 按执行命令分组。

软件默认值和负载参数在脚本中维护。

## 3. 在脚本中初始化必需变量

Framework 每次调用阶段函数都会通过环境变量传入下列值。脚本须读取并保留传入值；独立运行时才按右列设置默认值。示例中的变量赋值用于兼容这两种运行方式。

`SCRIPT_DIR` 由脚本位置计算，`SOFTWARE_NAME` 固定为注册名。

| 变量 | Framework 自动传入值 | 独立运行默认值 |
|---|---|---|
| `SOFTWARE_VERSION` | 任务选择的版本 | 已在 `case.yaml.versions` 声明并验证的版本。 |
| `EXPECTED_ARCH` | `x86_64` 或 `aarch64` | `uname -m` 的实际架构，并在构建前校验。 |
| `PERF_RUN_ID` | 本次任务 ID | 唯一的 `local-<UTC时间>-<进程号>`。 |
| `PERF_WORK_DIR` | `/home/runner/boostkit-perf/<software>/<version>/<architecture>/<run_id>/` | `/home/runner/boostkit-perf/<software>/` 下本次运行独占的子目录。 |
| `RESULTS_DIR` | 仓库下 `.perf-output/<category>/<software>/<version>/<architecture>/<run_id>/` | `<脚本目录>/results/<version>/<run_id>/`，可由调用者指定。 |
| `PERF_ACTUAL_VERSION_FILE` | `PERF_WORK_DIR/actual-version.txt` | `RESULTS_DIR/actual-version.txt`。 |
| `TMPDIR` | `PERF_WORK_DIR/tmp` | `PERF_WORK_DIR` 下的私有临时目录。 |

将以下初始化放在脚本中：

- 将 `example` 改为当前软件的注册名。
- 将 `1.0.0` 改为已验证的默认版本。
- 在四个阶段函数中调用 `configure_runtime_paths`。

```bash
#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_NAME="example"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.0.0}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"

configure_runtime_paths() {
    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || return 10
    RESULTS_DIR="${RESULTS_DIR:-${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}}"
    PERF_WORK_DIR="${PERF_WORK_DIR:-/home/runner/boostkit-perf/${SOFTWARE_NAME}/local-${PERF_RUN_ID}}"
    PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-${RESULTS_DIR}/actual-version.txt}"
    TMPDIR="${PERF_WORK_DIR}/tmp"
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID PERF_WORK_DIR RESULTS_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
    mkdir -p "${PERF_WORK_DIR}" "${RESULTS_DIR}" "${TMPDIR}"
}
```

每个阶段在新的 Shell 进程中运行。文件放置规则如下：

| 内容 | 目录 |
|---|---|
| 跨阶段的构建产物、配置、PID 和数据 | `PERF_WORK_DIR`。下一阶段从文件重新读取。 |
| 源码、安装、虚拟环境、缓存、高 I/O 数据、临时文件、服务 socket 和日志 | `PERF_WORK_DIR` 的子目录。 |
| 清单声明的原始输出和结构化结果 | `RESULTS_DIR`。 |

## 4. 实现获取、依赖和四阶段

安装缺失的系统依赖时：

- root 用户运行 `dnf install`；非 root Runner 运行 `sudo -n dnf`。
- 配置了 `PERF_PROXY` 时，向 dnf 传入 `--setopt=proxy=...`。

例如：

```bash
require_commands() {
    local packages=() dnf_options=()
    command -v cc >/dev/null 2>&1 || packages+=(gcc)
    command -v make >/dev/null 2>&1 || packages+=(make)
    command -v curl >/dev/null 2>&1 || packages+=(curl)
    if ! command -v sha256sum >/dev/null 2>&1 || \
       ! command -v tee >/dev/null 2>&1; then
        packages+=(coreutils)
    fi
    command -v python3 >/dev/null 2>&1 || packages+=(python3)
    (( ${#packages[@]} > 0 )) || return 0
    [[ -z "${PERF_PROXY:-}" ]] || dnf_options+=("--setopt=proxy=${PERF_PROXY}")
    if [[ "$(id -u)" -eq 0 ]]; then
        dnf "${dnf_options[@]}" install -y "${packages[@]}"
    else
        sudo -n dnf "${dnf_options[@]}" install -y "${packages[@]}"
    fi
}
```

获取安装包时按以下顺序：

1. 从 `/home/runner/software/<software>/` 读取预置离线包；该目录只作为输入。
2. 离线包不存在时，将网络下载文件保存到 `PERF_WORK_DIR`。
3. 按已核实的校验值验证，再在 `PERF_WORK_DIR` 中解包、构建和安装。

例如：

```bash
EXAMPLE_RELEASE_URL="<official-release-base-url>"
EXPECTED_SHA256="<verified-64-character-sha256>"
archive_name="example-${SOFTWARE_VERSION}.tar.gz"
archive="${PERF_WORK_DIR}/${archive_name}"
if [[ -f "/home/runner/software/${SOFTWARE_NAME}/${archive_name}" ]]; then
    cp "/home/runner/software/${SOFTWARE_NAME}/${archive_name}" "${archive}"
else
    curl -fSL --retry 3 -o "${archive}" "${EXAMPLE_RELEASE_URL}/${archive_name}"
fi
printf '%s  %s\n' "${EXPECTED_SHA256}" "${archive}" | sha256sum -c -
```

`EXAMPLE_RELEASE_URL` 和 `EXPECTED_SHA256` 须对应所选版本、架构的官方发布信息。源码仓库型软件在 `PERF_WORK_DIR` 克隆指定 tag 或提交，并核对实际提交。

四个阶段各自重新计算本阶段使用的路径：

| 函数 | 操作 | 必须留下的内容 |
|---|---|---|
| `build_example` | 初始化路径，校验架构，安装依赖，获取并校验版本，在 `PERF_WORK_DIR` 构建或安装。 | 从产物读取实际版本，写入 `PERF_ACTUAL_VERSION_FILE`。 |
| `start_example` | 初始化路径；服务型软件启动并有界等待就绪，无服务软件准备数据或确认 benchmark 可执行。 | 服务 PID、socket 等放在私有目录。 |
| `run_example_benchmarks` | 初始化路径，运行 README 中确定的正式命令并严格解析。 | `RESULTS_DIR/benchmark_raw.txt` 和 `RESULTS_DIR/benchmark.json`。 |
| `stop_example` | 停止本软件启动的服务，核对进程退出；无服务软件安全收口。 | 重复调用也能成功收口。 |

`build_example` 写版本的命令示例：

```bash
printf '%s\n' "${actual_version}" > "${PERF_ACTUAL_VERSION_FILE}"
```

例如 test 阶段调用已安装的 benchmark 与本目录解析器：

```bash
"${PERF_WORK_DIR}/install/bin/example-benchmark" --iterations 3 \
    2>&1 | tee "${RESULTS_DIR}/benchmark_raw.txt" || return 50
python3 "${SCRIPT_DIR}/scripts/parse_benchmark.py" \
    "${RESULTS_DIR}/benchmark_raw.txt" "${RESULTS_DIR}/benchmark.json" || return 50
```

脚本启用 `set -o pipefail`，使 benchmark 失败能从上述管道返回非零。解析器须完成：

- 校验指标齐全、唯一，且数值有效。
- 将跨架构固定负载写入 JSON 的 `parameters`，机器相关值写入 `runtime_context`。
- 保留原始指标名称、单位及必要换算的依据，使结果可追溯。

脚本加载时只定义变量和函数。直接执行时，`main()` 须：

1. 接受 `--version` 和 `--results-dir`。
2. 按顺序调用同一组阶段函数。
3. 在成功或失败后停止服务、校验结果，并清理本次创建的私有工作目录。

入口形式如下。完整的独立运行收口可参照 [LZ4 脚本](../software/HPC/lz4/lz4_test.sh) 的 `main()` 和 `run_lz4_standalone()`。

```bash
if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
```

独立入口保留 `RESULTS_DIR` 供检查，失败时返回非零退出码。复制到项目外后，仍使用该软件目录内的脚本和解析器完成测试。

## 5. 校验并验收

先在仓库根目录执行静态检查。示例中的路径和名称须替换为当前软件：

```bash
bash -n software/HPC/example/example_test.sh
python3 -m compileall -q software/HPC/example/scripts
python3 framework/catalog.py validate
python3 framework/catalog.py matrix --software example --version 1.0.0 --architecture all --pretty
```

在专用 Runner 上依次验证：

1. 直接运行脚本，指定持久结果目录。
2. 将完整软件目录复制到项目外，重跑独立入口。

例如：

```bash
bash software/HPC/example/example_test.sh --version 1.0.0 \
  --results-dir /home/runner/example-results/1.0.0
cp -a software/HPC/example /home/runner/example-standalone-check
bash /home/runner/example-standalone-check/example_test.sh --version 1.0.0 \
  --results-dir /home/runner/example-results/standalone-check
```

随后分别在 x86_64、aarch64 上执行同版本正式任务，核对：

- 两个架构的实际产物版本和完整原始输出。
- `case.yaml` 声明的结果、指标单位、固定参数和实际并发度。
- 双架构报告是否只对齐同一测试项。
- 失败测试后的服务退出状态和私有工作目录清理结果。
