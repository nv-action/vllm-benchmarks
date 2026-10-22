# Kubernetes 性能测试说明

本目录测试 Kubernetes（容器编排系统，官方仓库
`https://github.com/kubernetes/kubernetes`），在精确 release tag 上克隆
上游源码，以完全离线的 vendor 模式编译并执行其官方 `go test -bench`
基准，进行 x86_64 与 aarch64 开箱性能对比。软件入口为
`kubernetes_test.sh`，Framework 通过 `case.yaml` 调用其 `build`、`start`、
`test`、`stop` 四个阶段；直接执行该脚本也使用同一套阶段函数。

当前清单仅声明 Kubernetes `1.37.0`。新增版本时，必须同步更新
`case.yaml` 的版本声明，不能跳过版本校验。

## 构建与安装

这里的“构建”是从官方仓库按精确 tag 浅克隆源码，并编译基准二进制，
不安装集群组件（kubelet/apiserver 等），不改动系统 Go 环境：

1. 安装任务私有 Go 工具链：优先使用离线目录
   `/home/runner/software/golang/go<版本>.linux-<架构>.tar.gz`
   （默认 `GO_VERSION=1.26.7`，对应 `1.37.0` 的 `go.mod` 要求
   `go 1.26.0`；离线目录缺失时从 `go.dev/dl` 下载兜底），校验
   sha256 后解压到 `${PERF_WORK_DIR}/go-install`，经 `GOROOT`/`PATH`
   生效。不安装系统级 `golang`；`git`、`python3` 等运行时依赖缺失时
   仍由脚本通过 `dnf` 安装（非 root Runner 使用 `sudo -n dnf`）。
2. `git clone --depth 1 --branch v1.37.0` 克隆到本次任务工作目录。
3. `git describe --tags --exact-match` 校验 tag 必须精确等于 `v1.37.0`，
   校验失败立即退出；实际版本写入
   `results/<版本>/<运行 ID>/actual-version.txt`。
4. 上游仓库自带完整 `vendor/` 目录，保留其 `go.work`（workspace 模式）并
   强制 vendor 模式编译。注意不能设置 `GOWORK=off`：上游的
   `vendor/modules.txt` 按 workspace 语义生成（staging 模块的 replace 标记
   仅在多模块校验下一致），关闭 workspace 会触发
   `inconsistent vendoring`；而 workspace 模式下 `-mod=vendor` 同样使用
   根 `vendor/` 目录，可完全离线编译：

```bash
cd "${PERF_WORK_DIR}/kubernetes"
env GOFLAGS=-mod=vendor GOPROXY=off GOSUMDB=off GOTOOLCHAIN=local \
    go test -c -o "${PERF_WORK_DIR}/bin/<基准名>.test" ./<基准包>
```

`GOPROXY=off` 保证编译完全不访问网络，`GOTOOLCHAIN=local` 禁止隐式
下载其它工具链。工作目录仅供本次任务使用。

## 运行时启动

Kubernetes 基准为库级计算负载，无服务进程。`start` 阶段对每个基准
二进制执行冒烟运行（`-test.benchtime=1x`），验证二进制可在当前
Runner 上正常执行并输出基准结果，冒烟日志落在
`smoke_kubernetes.log`。

## 性能测试

选用的三个官方基准包均为纯计算负载，不依赖 etcd、容器运行时或
网络服务：

| 基准包 | 指标组 | 内容 |
|---|---|---|
| `pkg/apis/core/v1` | `api_conversion` | Pod 列表 core/v1 双向序列化转换（1~10000 个 Pod） |
| `pkg/registry/core/service/ipallocator` | `ip_allocator` | Service ClusterIP 位图分配（IPv4/IPv6） |
| `pkg/controller/nodeipam/ipam/cidrset` | `cidr_set` | 节点 CIDR 集合分配（IPv6 多掩码） |

每个基准以 `-test.benchtime=1s -test.count=1 -test.timeout=20m` 运行，
原始输出逐包写入 `benchmark_kubernetes_raw.log`（带 `### PACKAGE` 分节
标记）。可以脱离 Workflow 执行完整流程；脚本会依次执行 build、start、
test、stop 并保存同样的产物。例如：

```bash
bash software/Middleware/kubernetes/kubernetes_test.sh \
  --version 1.37.0 \
  --results-dir /home/runner/boostkit-perf/kubernetes/results/1.37.0
```

## 指标

保留各基准的原始测量值，不做平均、加权或跨包聚合。每个基准贡献其
输出的全部标准列：耗时（`ns/op` 换算为 us）、内存（`B/op`）、分配次数
（`allocs/op`），全部为越小越好。指标名形如
`BenchmarkPodListConversion/core-to-v1/pods=1000 time`，按
`api_conversion` / `ip_allocator` / `cidr_set` 三个指标组展示，便于在
相同负载下比较 x86_64 与 aarch64。测试工具及其固定版本会同时列在
报告的“测试环境”部分。

## 结果与清理

`case.yaml` 要求以下测试产物：

- `benchmark_kubernetes_raw.log`：逐包基准与聚合的完整控制台输出；
- `benchmark_kubernetes.json`：基准的结构化结果；
- `results.json`：全部结构化指标及其来源字段。

`stop` 阶段删除本次任务创建的工作目录（含源码树、基准二进制与私有
Go 工具链）。
基准二进制无守护进程，无需进程级回收；Framework 随后执行 Runner 级
环境清理。
