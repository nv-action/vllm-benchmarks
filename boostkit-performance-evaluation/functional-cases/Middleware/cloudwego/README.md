# CloudWeGo 0.16.4 功能用例原理

本用例验证固定版本的 CloudWeGo 组件能否完成源码校验、命令行工具运行、代码生成、生成项目编译，以及真实的本地 HTTP/RPC 调用。它不测吞吐量、延迟，也不把工具能打印版本号当作功能通过。执行入口和阶段定义见 [func_case.yaml](func_case.yaml)，实现分别在 [build.sh](build.sh)、[test.sh](test.sh) 与 [common.sh](common.sh)。

## 被测对象与版本

CloudWeGo 各组件独立发版。用例名中的 `0.16.4` 是 Kitex 源码标签版本，其余版本按参考 Dockerfile 固定，不能从套件名推断。

| 组件 | 固定源码标签 | 实际 CLI 期望输出中的版本 | 用途 |
| --- | --- | --- | --- |
| Kitex | `v0.16.4` | `0.16.3` | 生成 Thrift 服务、编译、RPC 回环与上游测试 |
| Hertz | `v0.10.6` | `hz 0.9.7` | 生成 HTTP 项目、编译、HTTP 回环与上游测试 |
| Volo | `volo-thrift-0.12.2` | `volo 0.12.1` | 生成并编译 Rust/Thrift 项目 |
| Eino | `v0.9.22` | 无 CLI | 编译及离线单元测试 |

CLI 版本来自对应标签内工具自身的版本常量，因此与源码标签不完全相同。[common.sh](common.sh) 分别维护这些期望值，`version` 检查按实际输出中的版本号判定。构建使用 Go `1.24.4`；只接受这个精确版本，缺失时在任务目录安装。`GOTOOLCHAIN=local` 阻止 Go 隐式切换工具链，`GOENV=off` 屏蔽宿主机 Go 配置，`CGO_ENABLED=0`。Go 模块、构建缓存和 Rust/Cargo 的下载及构建目录均在本次任务的工作目录。Rust/Cargo 使用系统包，未在用例中固定编译器版本。

## 工作流如何运行

[配置文件](../../../config/functional.yaml) 将 openEuler `24.03 SP4` 设为 `prebuilt`：镜像名按 `openeulertest/{software}:{version}-oe2403sp4-{arch}` 拼接；Ubuntu `26.04.1` 使用 `ubuntu:26.04` 基础镜像和 `build` 模式。工作流矩阵在鲲鹏 920、950 Runner 上选择 `aarch64` 架构，两个系统各执行一次。[func_case.yaml](func_case.yaml) 指定四个顺序阶段，整个用例超时为 90 分钟：

1. **prepare**：建立任务及结果目录，记录实际架构、系统、内核、CPU 数、Python/GCC/glibc 版本到 `system_info.json`。探测不到的版本记为 `unknown`，避免环境信息采集导致无解释退出。
2. **install**：先核对实际架构与预期架构、套件版本白名单，再补齐缺失的命令及构建依赖。Ubuntu 仅在需要安装依赖时执行 `apt-get update`，随后 `apt-get install`；没有 `apt-get upgrade` 或全量升级。`install` 仍可能升级所安装依赖关联的软件包，应以安装日志为准。openEuler 对应使用 `dnf install`。
3. **check**：按下文八项检查的固定顺序运行，逐项写日志和判定。某项失败后继续尝试其余检查，以便一次运行收集更多故障信息；依赖生成目录的后续项会明确失败。任一项失败，整体结果为 `failed`。
4. **cleanup**：删除临时 Hertz smoke 程序和生成项目目录。结果目录中的日志、版本、锁文件及 JSON 保留。`codegen-dir.txt` 仅供执行期间定位，清理后其中的临时路径不再可用。

### install 阶段的两种模式

两种模式都下载 Kitex、Hertz、Volo、Eino 的固定标签归档。每个归档先由 [verify_source.py](verify_source.py) 对照 [source-lock.json](source-lock.json) 核验标签及 SHA-256，**通过后才解压**；实际摘要写入 `source-sha256.txt`。归档保留到 `check` 阶段再次校验，因此安装后归档丢失或被改动会失败。哈希基线来自首次已验证构建使用的归档，不等同于上游签名认证。

- **Ubuntu build**：在任务私有目录用上述源码编译 Kitex `kitex`、Hertz `hz` 和 Volo `volo-cli`。Volo 的 CLI 构建执行 `cargo build --release --locked -p volo-cli`，依照该源码的 `Cargo.lock`；Eino 执行 `go mod download` 和 `go build ./...`。随后构建 Kitex `go.mod` 锁定的 `thriftgo`，供代码生成使用，避免 `hz` 缺失依赖时自动取得最新版本。
- **openEuler prebuilt**：Kitex、hz、volo 从镜像的 `/usr/local/bin` 取用，**不重新编译覆盖这些被测二进制**。固定归档解压出的 Kitex/Hertz 源码用于上游测试及生成项目的本地模块依赖。镜像已有的 `/eino` 与固定 Eino 归档逐文件比较，再对归档解压出的 Eino 执行构建；比较的是归档中存在的文件，未检查镜像 `/eino` 的额外文件。

两种模式最后都执行一次组件版本检查，并写入 `actual-version.txt`；`check` 阶段会再次检查。预构建模式的源码校验和 CLI 版本检查不能证明镜像内二进制一定由这些源码构建，功能检查负责验证其实际行为。

## check 阶段：输入、动作与通过条件

| 顺序 | 检查名 | 输入 | 执行动作 | 通过条件 |
| --- | --- | --- | --- | --- |
| 1 | `source-integrity` | 安装阶段保留的四份源码归档、[锁定清单](source-lock.json) | 重新核对每个组件的标签与归档 SHA-256。 | 四份归档均存在，标签和摘要均与清单一致。 |
| 2 | `version` | 实际 `kitex`、`hz`、`volo` CLI，解压后的 Eino 源码 | 执行三个 CLI 的 `--version`，解析输出中的版本号；读取 Eino 的源码标签记录。 | 三个 CLI 均可运行、版本号分别匹配上表，Eino 标签为 `v0.9.22`。 |
| 3 | `upstream-test` | 固定标签的 Kitex 与 Hertz 源码 | 分别运行 Kitex `go test -count=1 -timeout 5m ./client/...` 和 Hertz `go test -count=1 -timeout 5m ./pkg/...`。`-count=1` 禁用测试结果缓存。 | 两组测试均在限定时间内成功退出。这里只覆盖选定子树，不是全量上游测试。 |
| 4 | `smoke` | Hertz 源码模块中的临时 HTTP 程序 | 在本机临时端口启动 Hertz 服务，标准库客户端请求 `GET /ping`；等待服务最多 10 秒，单次请求超时 2 秒。 | 请求成功、HTTP 状态码为 200，且 JSON 的 `ping` 字段精确等于 `pong`。 |
| 5 | `codegen` | 本地 `echo.thrift`，实际 `kitex`、`hz`、`volo` CLI | IDL 定义含 `message` 字段的请求/响应、`Echo.Ping` 服务及 `/ping` HTTP 注解；依次调用三个 CLI 生成项目。 | 三个生成命令均成功，Kitex 的 `kitex_gen/echo/echo.go`、hz 的 `main.go`、Volo 的 `Cargo.toml` 和 `src/lib.rs` 均非空。下一项才验证可编译性。 |
| 6 | `generated-build` | 上一步生成的三个项目、本次固定源码、Volo 源码的 `Cargo.lock` | Kitex/hz 项目将框架模块 `replace` 到本次源码，执行 `go mod tidy` 和 `go build -mod=readonly ./...`。Volo 项目经 [pin_generated.py](pin_generated.py) 将 `volo`、`volo-thrift`、`volo-build` 指向本次源码，将 `pilota`、`anyhow`、`tokio` 固定到源码锁文件中的版本，再解析依赖并执行 `cargo build --locked`。 | 三个生成项目均编译成功。生成项目解析时可能更新间接依赖的锁定条目，不能据此证明完整依赖图逐字节复现。 |
| 7 | `kitex-rpc` | 生成的 Kitex 服务与客户端代码、[RPC 测试](kitex_rpc_test.go) | 在 `127.0.0.1` 临时 TCP 端口启动 `Echo.Ping`；客户端依次发送 `hello`、`中文 RPC`、空字符串。测试使用 `-count=1`、30 秒超时，单次 RPC 超时 1 秒，整体上下文超时 10 秒；结束时停止服务。 | 三次调用均成功，响应分别精确等于 `reply:` 加原消息，服务在 5 秒内退出。 |
| 8 | `eino-test` | 固定标签的 Eino 源码 | 离线执行 `go test -count=1 -timeout 5m ./schema/... ./components/prompt/...`，不调用外部模型 API。 | 两组测试均在限定时间内成功退出；不代表 Eino 全组件通过。 |

`codegen` 只检查关键文件存在，不能代替生成项目编译；`generated-build` 只验证编译，不能代替 Kitex 的真实 RPC 回环。三项连起来分别覆盖工具调用、生成代码的可编译性和实际服务交互。Hertz 的实际交互由独立 `smoke` 项覆盖；Volo 当前覆盖生成与编译，未运行 Volo RPC 服务。

## 结果与排障

`functional_result.json` 记录软件、版本、系统、运行模式、架构、运行 ID、总状态和八项逐项状态；`cloudwego_test.log` 汇总每项的通过/失败。各项的细节分别在同名日志中，例如 `generated-build.log` 和 `kitex-rpc.log`。CLI 输出保存在 `kitex-version.txt`、`hz-version.txt`、`volo-version.txt`；生成 Go 项目的模块文件、生成 Volo 项目的 `Cargo.lock` 与 Cargo 元数据也保存在结果目录，可用于核对实际解析依赖。所有八项均为 `passed` 时用例才通过。

本地 Ubuntu 26.04 / x86_64 的八项检查已通过。复用缓存的完整回归约 43 秒；首次生成 Volo 项目的 Rust 编译约 2 分 51 秒，首次 Volo CLI 的 Cargo 下载及 release 构建约 21 分 36 秒，耗时依赖网络与 CPU。源码归档篡改、生成 Go 代码语法错误、RPC 响应错误的反例均已验证会失败。本次测试环境初始化时 APT 将 `liblzma5` 升级了一个版本，未执行全量升级。

本地 openEuler 24.03 LTS-SP4 / x86_64 预构建镜像 `openeulertest/cloudwego:0.16.4-oe2403sp4-x86_64` 的八项检查也已通过。首次尝试从 GitHub 下载 Eino 源码归档超时；成功运行改用先前保存、经本用例 SHA-256 基线再次校验的四份归档作为本地下载源，其余安装、检查、清理逻辑不变。

[鲲鹏 920 的 ARM64 流水线](https://github.com/FdeCad/boostkit-performance-evaluation/actions/runs/39539010527) 对提交 `b502aa8` 执行了 openEuler 预构建和 Ubuntu 构建两种模式：矩阵报告为 2/2 通过，两个结果文件中的八项检查也均为 `passed`。该次流水线未覆盖鲲鹏 950。
