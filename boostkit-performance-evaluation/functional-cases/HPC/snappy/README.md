# Snappy 1.1.7 功能用例原理

本用例验证固定版本的 Snappy 源码归档完整性、被测交付物版本与真实库行为:归档的 tag 与 SHA-256 必须匹配仓库内基线,被测 `SnappyConfigVersion.cmake` 必须报告请求版本,链接被测 `libsnappy` 的 C++ 程序必须完成 Compress/Uncompress 回环。它不测压缩率、吞吐量,也不把库文件存在当作功能通过。执行入口和阶段定义见 [func_case.yaml](func_case.yaml),实现分别在 [build.sh](build.sh)、[test.sh](test.sh) 与 [common.sh](common.sh)。

## 被测对象与版本

待测版本为 `1.1.7`,与参考镜像的构建来源 `openeuler-docker-images/Others/snappy/1.1.7/24.03-lts-sp4/Dockerfile` 固定对齐,不自动跟随最新版本,新增版本需同批核对 [source-lock.json](source-lock.json) 与 [build.sh](build.sh) 中的版本校验。

| 被测交付物 | 固定来源 | 实际交付物中的版本 | 用途 |
| --- | --- | --- | --- |
| 源码归档 | GitHub tag `1.1.7` tar.gz,SHA-256 基线见 [source-lock.json](source-lock.json) | — | `source-integrity` 检查 |
| 库文件 | `libsnappy.so.1.1.7`,位于 `lib64`(openEuler)或 `lib`(Ubuntu) | `1.1.7` | `version` 存在性判定与 `roundtrip` 链接 |
| 版本元数据 | `cmake/Snappy/SnappyConfigVersion.cmake` | `PACKAGE_VERSION 1.1.7` | `version` 检查判定 |
| 头文件 | `snappy.h`、`snappy-c.h`、`snappy-sinksource.h`、`snappy-stubs-public.h` | — | `roundtrip` 编译 |

版本元数据与库文件名都由构建时的 CMake 从 `project(Snappy VERSION 1.1.7)` 与目标 `VERSION` 属性生成,因此互相一致;`version` 检查按 `SnappyConfigVersion.cmake` 的实际值判定。snappy 1.1.7 的 CMake 构建不生成 `snappy.pc`(仅 autoconf 构建提供),镜像 README 的 `pkg-config` 用法没有对应交付物,本用例不依赖 `pkg-config` 命令。库目录取 GNUInstallDirs 的实际结果:openEuler(RedHat 布局,amd64 与 arm64 一致)为 `lib64`,Ubuntu(Debian 布局)为 `lib`;参考 Dockerfile 的 `ld.so.conf.d/snappy.conf` 与 `PKG_CONFIG_PATH` 也对齐 `lib64`。参考构建流程不含上游测试:1.1.7 源码树没有 googletest 子模块,其 `snappy_unittest` 依赖系统 GTest,镜像只交付库、头文件与 CMake 元数据,因此本用例不设 `upstream-test` 检查项,库的实际行为由 `roundtrip` 直接验证(此项已与维护者确认)。

## 工作流如何运行

[配置文件](../../../config/functional.yaml) 将 openEuler `24.03 SP4` 设为 `prebuilt`:镜像名按 `openeulertest/snappy:1.1.7-oe2403sp4-{arch}` 拼接;Ubuntu `26.04.1` 使用 `ubuntu:26.04` 基础镜像和 `build` 模式。工作流矩阵在鲲鹏 920、950 Runner 上选择 `aarch64` 架构,两个系统各执行一次。[func_case.yaml](func_case.yaml) 指定四个顺序阶段,整个用例超时为 30 分钟:

1. **prepare**:建立任务及结果目录,记录实际架构、系统、内核、CPU 数、Python/GCC/glibc 版本到 `system_info.json`。探测不到的版本记为 `unknown`,避免环境信息采集导致无解释退出。
2. **install**:先核对实际架构与预期架构、版本白名单,再补齐缺失的命令(下载、解压、编译与 CMake),补齐依赖后才下载归档:精简基础镜像(如 ubuntu:26.04)可能缺 `curl`/`python3`。Ubuntu 仅在需要安装依赖时执行 `apt-get update`,随后 `apt-get install`;没有 `apt-get upgrade` 或全量升级。openEuler 对应使用 `dnf install`。
3. **check**:按下文三项检查的固定顺序运行,逐项写日志和判定。某项失败后继续尝试其余检查,以便一次运行收集更多故障信息;任一项失败,整体结果为 `failed`。
4. **cleanup**:删除本次生成的回环程序目录。结果目录中的日志、版本文件及 JSON 保留;归档、源码与构建树由框架在 cleanup 阶段统一回收。

### install 阶段的两种模式

两种模式都下载 1.1.7 官方 tag 归档,curl 参数与参考 Dockerfile 一致。归档先由 [verify_source.py](verify_source.py) 对照 [source-lock.json](source-lock.json) 核验标签及 SHA-256,**通过后才解压**;实际摘要写入 `source-sha256.txt`。归档保留到 `check` 阶段再次校验,因此安装后归档丢失或被改动会失败。哈希基线取自参考 Dockerfile 的校验值,不等同于上游签名认证。

- **Ubuntu build**:在任务私有目录解压归档,照参考 Dockerfile 的参数执行 `cmake -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX=/usr/local -DBUILD_SHARED_LIBS=ON`、`make -j$(nproc)`,再 `make install DESTDIR` 到私有安装目录,不写入系统路径。另加两个环境兼容开关:`-DSNAPPY_BUILD_TESTS=OFF`(参考环境该选项默认 ON 的实际产物是无 gtest 的空壳测试且 `make install` 从不安装测试;关闭后用例也能在装有系统 GTest 的机器上独立运行,规避 1.1.7 上游 `GTEST_LIBRARIES` 链接缺陷)与 `-DCMAKE_POLICY_VERSION_MINIMUM=3.5`(让 CMake 4 接受 1.1.7 的 `cmake_minimum_required(3.1)`,旧 CMake 下该开关仅是无害缓存变量)。
- **openEuler prebuilt**:库、头文件与 `SnappyConfigVersion.cmake` 从镜像的 `/usr/local` 取用,**不重新编译覆盖被测库**;归档下载仅供 `source-integrity` 复核。依赖安装只补齐归档下载/校验与回环程序编译所需的命令,不改动 libsnappy 本身。

两种模式最后都执行一次交付物版本检查,并写入 `actual-version.txt`;`check` 阶段会再次检查。预构建模式的源码校验不能证明镜像内二进制一定由这些源码构建,功能检查负责验证其实际行为。

## check 阶段:输入、动作与通过条件

| 顺序 | 检查名 | 输入 | 执行动作 | 通过条件 |
| --- | --- | --- | --- | --- |
| 1 | `source-integrity` | install 阶段保留的源码归档、[锁定清单](source-lock.json) | 重新核对归档的标签与 SHA-256。 | 归档存在,标签和摘要均与清单一致。 |
| 2 | `version` | 被测 `SnappyConfigVersion.cmake`、带版本号的库文件 | 解析 `set(PACKAGE_VERSION "x.y.z")`;确认带版本号的库文件存在。 | 版本号精确等于 `1.1.7`,库文件存在。 |
| 3 | `roundtrip` | 被测库、被测头文件 | 用 `c++ -I<前缀>/include -L<前缀>/<库目录> -lsnappy` 编译确定性数据(120000 字节)的 Compress/Uncompress 回环程序并运行。prebuilt 模式不加 rpath,运行时由镜像的 ldconfig 解析 SONAME,直接检验参考 Dockerfile 运行层的行为;build 模式库在私有目录,用 rpath 定位。 | 编译成功,解压结果与原始输入逐字节一致。 |

`version` 只证明交付物元数据与版本号一致,不能证明库可用;`roundtrip` 才验证链接、加载与实际压缩行为。两项合起来覆盖参考 Dockerfile 交付的 CMake 版本文件、头文件、库与 ldconfig 整条链路。

## 结果与排障

`functional_result.json` 记录软件、版本、系统、运行模式、架构、运行 ID、总状态和三项逐项状态;`snappy_test.log` 汇总每项的通过/失败。各项的细节分别在同名日志中,例如 `roundtrip.log`。`checks.tsv` 为逐项判定的原始记录;`source-sha256.txt` 记录 install 阶段实测的归档摘要,可与基线核对。所有三项均为 `passed` 时用例才通过。

本地 Ubuntu 26.04 / x86_64 容器内按工作流流程(四阶段经 [run_func_case.py](../../../functional-framework/run_func_case.py) 驱动)的运行已通过;篡改归档(仅 `source-integrity` 失败)与篡改版本文件(仅 `version` 失败)的反例均已验证会如实失败。

本地 openEuler 24.03 LTS-SP4 / x86_64 预构建镜像 `openeulertest/snappy:1.1.7-oe2403sp4-x86_64` 当前 `/usr/local` 下只有头文件、没有库,用例在 install 定位交付物时如实失败。其根因是参考 Dockerfile 运行层的 `ld.so.conf.d/snappy.conf` 与 `PKG_CONFIG_PATH` 指向 `/usr/local/lib`,而库按 GNUInstallDirs 落在 `/usr/local/lib64`,运行层两处路径已修正为 `lib64`;按修正后 Dockerfile 构建的镜像三项检查全部通过。镜像仓库重新发布该镜像前,openEuler prebuilt 单元会保持失败。
