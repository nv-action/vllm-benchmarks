# 本地验证测试时间更新功能教学指南

本指南将指导你如何在本地容器环境中运行部分测试，生成执行统计数据，并验证 `update_test_times.py` 脚本是否能按预期逻辑更新 `config.yaml`。

## 1. 环境准备

首先，确保你处于一个配置好昇腾 (Ascend) 环境的 Docker 容器中。

### 1.1 进入容器
```bash
# 示例：启动并进入 CI 使用的镜像
docker run -it --rm \
  --device=/dev/davinci0 \
  --device=/dev/davinci_manager \
  --device=/dev/devmm_svm \
  --device=/dev/hisi_hdc \
  -v /usr/local/Ascend/driver:/usr/local/Ascend/driver \
  -v $(pwd):/work \
  -w /work \
  swr.cn-southwest-2.myhuaweicloud.com/base_image/ascend-ci/cann:8.5.0-910b-ubuntu22.04-py3.11 \
  /bin/bash
```

### 1.2 安装必要依赖
在容器内部，安装测试和 YAML 处理所需的库：
```bash
pip install -r requirements-dev.txt
pip install ruamel.yaml tabulate
pip install -e .
```

---

## 2. 模拟/运行部分测试并生成数据

为了验证脚本，我们需要生成一个 `test_stats.json` 文件。我们将运行 `e2e-singlecard` 套件中的一两个测试。

### 2.1 设置环境变量
脚本依赖 `VLLM_COLLECT_TEST_STATS` 环境变量来决定是否保存数据。
```bash
export VLLM_COLLECT_TEST_STATS=1
export VLLM_TEST_STATS_FILE=test_stats.json
```

### 2.2 运行特定的测试分片
为了节省时间，我们可以使用 `auto-partition` 功能只运行一小部分测试。例如，假设我们将 `e2e-singlecard-light` 套件分成 10 份，只运行第 0 份：
```bash
python3 .github/workflows/scripts/run_suite.py \
  --suite e2e-singlecard-light \
  --auto-partition-id 0 \
  --auto-partition-size 1
```
运行完成后，当前目录下会生成一个 `test_stats.json` 文件。

---

## 3. 验证更新功能

现在我们有了实际的执行数据，可以验证 `update_test_times.py` 的逻辑了。

### 3.1 检查当前配置
查看 `.github/workflows/scripts/config.yaml` 中相关测试的初始 `estimated_time`。

### 3.2 运行更新脚本 (演练模式)
首先使用 `--dry-run` 查看脚本计划进行的修改，而不实际写入文件：
```bash
python3 .github/workflows/scripts/update_test_times.py \
  --config .github/workflows/scripts/config.yaml \
  --stats test_stats.json \
  --dry-run
```

### 3.3 验证修改逻辑
根据你修改的代码，验证以下三点：
1. **覆盖逻辑**：如果 `config.yaml` 中某个测试的时间是 `0`，脚本必须将其更新。
2. **偏差 > 30s 逻辑**：如果 `test_stats.json` 中的实际时间与原时间差距 **超过 30 秒**，脚本应更新该值。
    - *技巧：你可以手动编辑 `test_stats.json` 中的某个 duration，使其比原时间大 40s，然后运行脚本。*
3. **偏差 <= 30s 逻辑**：如果差距在 **30 秒以内**，即使有波动，脚本也应该输出 "No updates needed" 或跳过该项。

### 3.4 执行实际更新
确认逻辑无误后，去掉 `--dry-run`：
```bash
python3 .github/workflows/scripts/update_test_times.py \
  --config .github/workflows/scripts/config.yaml \
  --stats test_stats.json
```

---

## 4. 结果确认

使用 `git diff` 查看 `config.yaml` 的变动：
```bash
git diff .github/workflows/scripts/config.yaml
```

**预期输出示例：**
- 如果 `test_stats.json` 显示耗时 120s，原配置是 80s（差距 40s > 30s），你应该看到：
  ```diff
  -    estimated_time: 80
  +    estimated_time: 120
  ```
- 如果原配置是 100s，实际耗时 110s（差距 10s < 30s），则不应有任何 diff。

## 5. 提示
如果你想强制触发更新进行观察，可以直接手动修改 `config.yaml` 中的某个值为 `0` 或一个非常小的值（如 `1`），然后重新运行第 3 步的脚本。
