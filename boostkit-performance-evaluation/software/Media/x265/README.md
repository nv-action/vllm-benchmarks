# x265 开箱编码性能测试

测试版本为 `4.2`。入口是 `x265_test.sh`，四阶段函数由 `case.yaml` 声明。测试场景参考[开箱测试方案](https://atomgit.com/weixin_51237760/tasks/tree/main/2026/0924-x264%E3%80%81x265%E5%BC%80%E7%AE%B1)。

## 构建与安装

脚本按参考资料从 [x265 官方下载目录](https://bitbucket.org/multicoreware/x265_git/downloads)获取 `x265_4.2.tar.gz` 并解压。缺少构建命令时用 `dnf` 安装，x86_64 还需要 NASM；`PERF_PROXY` 非空时作为 dnf 代理。

关键命令（在本次私有工作目录 `PERF_WORK_DIR` 下执行）：

```bash
cd "$PERF_WORK_DIR"
INSTALL_DIR="$PERF_WORK_DIR/x265_4.2/x265_install"
wget https://bitbucket.org/multicoreware/x265_git/downloads/x265_4.2.tar.gz
tar -zxvf x265_4.2.tar.gz
cd x265_4.2
mkdir x265_build x265_install
cd x265_build
cmake -S ../source/ \
  -DCMAKE_BUILD_TYPE=Release -DENABLE_PIC=ON -DENABLE_ASSEMBLY=ON \
  -DCMAKE_VERBOSE_MAKEFILE=ON -DCMAKE_C_FLAGS=-O3 -DCMAKE_CXX_FLAGS=-O3 \
  -DHIGH_BIT_DEPTH=off -DENABLE_TESTS=on \
  -DENABLE_SHARED=on -DENABLE_LIBNUMA=off \
  -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR"
make -j"$(nproc)" && make install
export LD_LIBRARY_PATH="$INSTALL_DIR/lib:${LD_LIBRARY_PATH:-}"
export PATH="$INSTALL_DIR/bin:$PATH"
x265 --version
```

脚本验证实际编码器版本以 `4.2` 开头，不进行系统级安装。

## 视频素材

在 Runner 持久目录 `/home/runner/software/x265/video/` 放入与 [x264 说明](../x264/README.md#视频素材)列出名称相同的 13 个 `.yuv` 文件。素材必须是 1920×1080、8 位 I420 的完整帧；两个架构上的文件须逐一相同。脚本记录 SHA-256、文件长度及使用帧数，以校验跨架构负载。

## 测试与指标

测试阶段直接执行未修改的参考脚本 [fps-encode-x265.sh](scripts/fps-encode-x265.sh) 和 [score-x265.sh](scripts/score-x265.sh)。脚本需要的 `TEST_HOME/video` 是指向上述 Runner 持久目录的符号链接，`TEST_HOME` 为本次私有工作目录。

调用方式与原文一致：aarch64 使用 `bash fps-encode-x265.sh 8core 920 1`，x86_64 使用 `bash fps-encode-x265.sh 8core x86 1`，随后执行 `bash score-x265.sh`。原脚本会对每个视频以 `2000`、`4000`、`6000`、`8000` kb/s 编码，共 52 个场景，固定传入 `--frames 9999`。CPU 绑定、线程池及编码参数均由原脚本决定；x86 的 CPU 偏移量也保留原脚本的 `192`。

参考命令形态（实际值随视频变化）：

```bash
taskset -c <本路 CPU 列表> "$INSTALL_DIR/bin/x265" \
  --preset faster --input /home/runner/software/x265/video/Dota2_1920x1080_60.yuv \
  --input-res 1920x1080 --fps 60 --frames 9999 \
  --keyint 120 --min-keyint 60 --bframes 0 \
  --bitrate 2000 --vbv-maxrate 2000 --vbv-bufsize 2000 \
  --rc-lookahead 1 --frame-threads 4 --pools 16 --lookahead-threads 3 \
  -o <本次任务的私有码流路径>
```

报告指标取原脚本生成的 `video_fps_summary_x265.csv` 中的 `Total FPS`，单位 `fps`，越高越好；按码率分组。结果转换程序只校验原始日志与 CSV，并生成 `benchmark_x265.json` 供框架读取，不执行编码。原始日志合并到 `benchmark_encode.txt`；码流文件留在本次私有工作目录，独立运行结束时由现有清理流程处理。

独立运行：

```bash
bash software/Media/x265/x265_test.sh --version 4.2 \
  --results-dir /home/runner/x265-results
```
