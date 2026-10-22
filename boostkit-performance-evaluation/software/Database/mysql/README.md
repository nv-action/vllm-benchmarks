# MySQL 8.0.43 性能测试

本用例在 x86_64、aarch64 上部署 Oracle MySQL 官方 Linux Generic 二进制包，
使用同一套单机 Sysbench OLTP 参数进行开箱对比。入口为 `mysql_test.sh`；
Framework 按 `build`、`start`、`test`、`stop` 调用脚本中的阶段函数。

## 软件与测试工具

| 项目 | 来源与版本 | 安装位置 |
|---|---|---|
| MySQL | Oracle MySQL 8.0.43 Linux Generic 二进制包 | `${PERF_WORK_DIR}/mysql` |
| Sysbench | [上游 Sysbench](https://github.com/akopytov/sysbench) 1.0.17，提交 `d634bce` | `${PERF_WORK_DIR}/sysbench-tools/sysbench-1.0.17` |

Sysbench 在私有目录编译，不安装到系统目录。脚本以 `src/lua/` 中的绝对路径
调用 OLTP 用例，并设置 `LUA_PATH` 供用例加载同目录的 `oltp_common.lua`；
开始准备数据前会验证三个用例都能加载。

## MySQL 获取、部署与启动

MySQL 包优先取 `/home/runner/software/mysql/` 中的对应架构离线包；没有时，
使用本任务目录的同名缓存包；仍没有则从
`https://dev.mysql.com/get/Downloads/MySQL-8.0/` 下载。

| 架构 | 包名 | 预期 MD5 |
|---|---|---|
| `x86_64` | `mysql-8.0.43-linux-glibc2.28-x86_64.tar.xz` | `9b6ef0d08e5506eab12fe2c1b619f664` |
| `aarch64` | `mysql-8.0.43-linux-glibc2.28-aarch64.tar.xz` | `c51923a9b540cd2f886734d3e0366e9a` |

`build_mysql` 使用下列命令形式部署，`archive_path` 为实际选中的包，
`PERF_WORK_DIR` 为 Framework 分配的本次任务目录：

```bash
curl -fSL --retry 3 --connect-timeout 30 \
  -o "${PERF_WORK_DIR}/${archive_name}" \
  "https://dev.mysql.com/get/Downloads/MySQL-8.0/${archive_name}" # 仅本地包和缓存都不存在时
md5sum "${archive_path}"  # 必须匹配上表；否则中止
tar -xJf "${archive_path}" -C "${PERF_WORK_DIR}"
mv "${PERF_WORK_DIR}/mysql-8.0.43-linux-glibc2.28-${EXPECTED_ARCH}" \
  "${PERF_WORK_DIR}/mysql"
"${PERF_WORK_DIR}/mysql/bin/mysqld" --version
```

实际脚本先检查本地包、校验 MD5，再解压并核对 `mysqld` 的实际版本；
不会编译 MySQL，也不会向 `/usr/local/mysql` 或系统目录安装。

`start_mysql_service` 在 `${PERF_WORK_DIR}/data/` 创建一次性实例。以下是脚本
的初始化和数据准备模式启动命令；路径、端口、运行用户和密码都属于本次任务：

```bash
"${MYSQLD_BIN}" --no-defaults --initialize-insecure \
  --basedir="${MYSQL_BASE_DIR}" --datadir="${DATADIR}" \
  --user="$(id -un)" --log-error="${ERR_LOG}"

"${MYSQLD_BIN}" --no-defaults --daemonize \
  --basedir="${MYSQL_BASE_DIR}" --datadir="${DATADIR}" \
  --tmpdir="${MYSQL_TMP_DIR}" --user="$(id -un)" \
  --socket="${SOCKET_PATH}" --pid-file="${PID_FILE}" \
  --port="${MYSQL_PORT}" --bind-address="${MYSQL_HOST}" \
  --max-connections=2048 --max-prepared-stmt-count=1048576 \
  --log-error="${ERR_LOG}" \
  --innodb-flush-log-at-trx-commit=0 --innodb-use-native-aio=OFF --skip-log-bin
```

初始化后，脚本经私有 socket 给 `root` 设置本次任务密码、创建 `sbtest`，
再通过 `127.0.0.1:<任务端口>` 执行 `SELECT 1` 验证连接。任务端口由运行 ID
派生，不固定占用 `3306`；已有实例占用该端口时会拒绝继续测试。

```bash
"${MYSQL_BIN}" --socket="${SOCKET_PATH}" -u root <<SQL
ALTER USER 'root'@'localhost' IDENTIFIED BY '${MYSQL_PASSWORD}';
CREATE USER IF NOT EXISTS 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
ALTER USER 'root'@'%' IDENTIFIED BY '${MYSQL_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'%' WITH GRANT OPTION;
CREATE DATABASE IF NOT EXISTS sbtest;
FLUSH PRIVILEGES;
SQL

MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQL_BIN}" \
  -h"${MYSQL_HOST}" -P"${MYSQL_PORT}" -uroot -N -e 'SELECT 1'
```

数据准备完成后，脚本先用本任务 socket 正常停库、复制数据快照，再以相同的
私有目录和连接参数启动正式测试模式：

```bash
MYSQL_PWD="${MYSQL_PASSWORD}" "${MYSQLADMIN_BIN}" \
  --socket="${SOCKET_PATH}" -u root shutdown

"${MYSQLD_BIN}" --no-defaults --daemonize \
  --basedir="${MYSQL_BASE_DIR}" --datadir="${DATADIR}" \
  --tmpdir="${MYSQL_TMP_DIR}" --user="$(id -un)" \
  --socket="${SOCKET_PATH}" --pid-file="${PID_FILE}" \
  --port="${MYSQL_PORT}" --bind-address="${MYSQL_HOST}" \
  --max-connections=2048 --max-prepared-stmt-count=1048576 \
  --log-error="${ERR_LOG}" \
  --innodb-flush-log-at-trx-commit=1 --innodb-use-native-aio=ON \
  --log-bin="${DATADIR}/mysql-bin"
```

正式测试前会查询并保存 `innodb_flush_log_at_trx_commit`、
`innodb_use_native_aio`、`log_bin`、`max_connections` 和
`max_prepared_stmt_count` 的实际生效值。两种启动模式均校验预处理语句上限为
`1048576`，以支持 64 张表、多线程的 Sysbench OLTP 场景；该值只是并发语句
数量的上限，不会预先创建这么多语句。
测试结束后再次通过任务 socket 正常关闭 MySQL，并清理私有数据目录。

## Sysbench 构建

Sysbench 从上游固定标签克隆、核对提交，再在本次任务目录编译：

```bash
git clone --depth 1 --branch 1.0.17 https://github.com/akopytov/sysbench.git sysbench-1.0.17
cd sysbench-1.0.17
./autogen.sh
PATH="<本次任务的 MySQL bin 目录>:$PATH" ./configure --with-mysql
make -j"$(getconf _NPROCESSORS_ONLN)"
```

缺失的系统运行和编译依赖由脚本使用 `dnf` 安装；普通 Runner 用户使用
`sudo -n dnf install -y <缺失的包>`，例如 `libaio-devel`、`openssl-devel`、
`gcc`、`make`、`autoconf`、`automake`、`libtool`。测试仅连接本任务启动、绑定
`127.0.0.1` 的 MySQL 实例，不会修改系统 `/etc/my.cnf` 或系统 MySQL。

## 数据与运行顺序

数据、临时文件和快照均在 `/home/runner/boostkit-perf/mysql/<本次任务>/` 下，
使用 Runner 的实际磁盘，不使用 `/tmp` 存放数据库。两种启动模式均使用
`--no-defaults`，不应用参考配置中的 230G buffer pool 等特定硬件调优参数。

1. 初始化私有实例并建库 `sbtest`。
2. 运行 Sysbench `oltp_read_write prepare`：64 张表，每张 10,000,000 行，
   256 个准备线程。
3. 停止 MySQL，在私有目录复制一份已准备的数据快照，再按正式配置重启。
4. 按顺序测试 `read_only`、`read_write`、`write_only`。每个场景先用
   `oltp_read_only`、64 线程预热 300 秒，再运行一轮正式测试。读写场景开始前
   重启服务；只写场景开始前从准备数据快照恢复，然后重启服务。
5. 每次正式测试持续 120 秒，记录每秒进度和完整 Sysbench 汇总。只有进入
   只写场景时恢复准备数据快照。

命令形态如下，`<连接参数>` 是本任务生成的主机、端口、账号和密码；
`SYSBENCH_LUA_DIR` 为 `${PERF_WORK_DIR}/sysbench-tools/sysbench-1.0.17/src/lua`：

```bash
LUA_PATH="${SYSBENCH_LUA_DIR}/?.lua;;" \
  "${SYSBENCH_BIN}" --db-driver=mysql <连接参数> --mysql-db=sbtest \
  --table_size=10000000 --tables=64 --threads=256 \
  "${SYSBENCH_LUA_DIR}/oltp_read_write.lua" prepare

LUA_PATH="${SYSBENCH_LUA_DIR}/?.lua;;" \
  "${SYSBENCH_BIN}" --db-driver=mysql <连接参数> --mysql-db=sbtest \
  --table_size=10000000 --tables=64 --threads=64 --time=300 \
  --percentile=95 --report-interval=1 \
  "${SYSBENCH_LUA_DIR}/oltp_read_only.lua" run

LUA_PATH="${SYSBENCH_LUA_DIR}/?.lua;;" \
  "${SYSBENCH_BIN}" --db-driver=mysql <连接参数> --mysql-db=sbtest \
  --table_size=10000000 --tables=64 --threads=<线程数> --time=120 \
  --percentile=95 --report-interval=1 \
  "${SYSBENCH_LUA_DIR}/oltp_<场景>.lua" run
```

| 场景 | 线程数（一轮） |
|---|---|
| `read_only` | 1、32、128、256 |
| `read_write` | 1、32、128、256 |
| `write_only` | 1、32、128、256 |

合计 12 次正式运行，仅正式计时约 24 分钟；三次预热另需 15 分钟，
准备、复制数据和服务重启还会增加时间。64 × 1000 万行及快照需要大量磁盘
空间，脚本在复制快照前检查可用空间。两架构必须使用相同矩阵，才会形成对照。

## 指标与产物

每次正式运行取 Sysbench 原始汇总的三个字段，不把预热结果纳入报告：

| 指标 | Sysbench 来源 | 单位 | 方向 |
|---|---|---|---|
| TPS | `transactions: ... (X per sec.)` | transactions/s | 越大越好 |
| QPS | `queries: ... (X per sec.)` | queries/s | 越大越好 |
| P95 latency | `Latency (ms)` 下的 `95th percentile` | ms | 越小越好 |

总计 `12 × 3 = 36` 个指标。报告按场景分表，每行是线程数，列为
TPS、QPS、P95 latency。`sysbench_raw.log` 保存完整控制台输出，
`sysbench-runs/` 保存每次运行的独立原始输出，`results.json` 保存结构化结果
及原始文件名。解析器要求 12 次运行全部完成、每个字段恰好出现一次；缺失或
异常值会使测试失败，不会生成部分成功的比较报告。

脱离 Workflow 执行完整流程：

```bash
bash software/Database/mysql/mysql_test.sh \
  --version 8.0.43 \
  --results-dir /home/runner/mysql-results/8.0.43
```

`stop` 阶段正常关闭本任务的 MySQL，并清理其私有数据目录。独立运行时默认
清理整个私有工作目录；如需排错，可加 `--keep-workdir`。
