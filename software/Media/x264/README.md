# x264 开箱编码性能测试

测试版本为 `0.165.3223`。入口是 `x264_test.sh`，四阶段函数由 `case.yaml` 声明。测试场景参考[开箱测试方案](https://atomgit.com/weixin_51237760/tasks/tree/main/2026/0924-x264%E3%80%81x265%E5%BC%80%E7%AE%B1)。

## 构建与安装

脚本按参考资料从 [VideoLAN x264 官方仓库](https://code.videolan.org/videolan/x264)克隆源码，不指定分支或提交。缺少构建命令时用 `dnf` 安装，x86_64 还需要 NASM；`PERF_PROXY` 非空时作为 dnf 代理。

关键命令（在本次私有工作目录 `PERF_WORK_DIR` 下执行）：

```bash
cd "$PERF_WORK_DIR"
INSTALL_DIR="$PERF_WORK_DIR/x264/x264_install"
git clone https://code.videolan.org/videolan/x264.git
cd x264
mkdir x264_build x264_install
cd x264_build
../configure --enable-shared --enable-pic --prefix="$INSTALL_DIR"
make -j"$(nproc)" && make install
export LD_LIBRARY_PATH="$INSTALL_DIR/lib:${LD_LIBRARY_PATH:-}"
export PATH="$INSTALL_DIR/bin:$PATH"
x264 --version
```

源码与安装目录均在本次 `PERF_WORK_DIR` 内，不覆盖系统编码器。

## 视频素材

将下列 13 个未经压缩的 1920×1080、8 位 I420 视频放在 Runner 持久目录 `/home/runner/software/x264/video/`，文件名以 `.yuv` 结尾。x265 使用相同内容，放在 `/home/runner/software/x265/video/`。两个架构上的视频必须逐一相同；脚本记录 SHA-256、文件长度及使用帧数，汇总时据此校验负载一致。文件不能缺失或包含不完整帧。

```text
Dota2_1920x1080_60.yuv
xihuanni_1920x1080_30_1000.yuv
showSingText_1920x1080_40.yuv
1_Yuanshen_1920x1080_60_8bit_12M.yuv
BQTerrace_1920x1080_60.yuv
Film2012_1920x1080_25_8bit_8Mbps.yuv
BQTerrace_1920x1080_60-dianbo.yuv
messi_1920x1080_25_1000.yuv
BasketballDrive_1920x1080_50.yuv
BQTerrace_1920x1080_60-biaozhun.yuv
Cactus_1920x1080_50.yuv
Kimono_1920x1080_24.yuv
ParkScene_1920x1080_24.yuv
```

## 测试与指标

测试阶段直接执行未修改的参考脚本 [fps-encode-x264.sh](scripts/fps-encode-x264.sh) 和 [score-x264.sh](scripts/score-x264.sh)。脚本需要的 `TEST_HOME/video` 是指向上述 Runner 持久目录的符号链接，`TEST_HOME` 为本次私有工作目录。

调用方式与原文一致：aarch64 使用 `bash fps-encode-x264.sh 8core 920 1`，x86_64 使用 `bash fps-encode-x264.sh 8core x86 1`，随后执行 `bash score-x264.sh`。原脚本会对每个视频以 `2000`、`4000`、`6000`、`8000` kb/s 编码，共 52 个场景，固定传入 `--frames 9999`。CPU 绑定、线程选择及编码参数均由原脚本决定；x86 的 CPU 偏移量也保留原脚本的 `192`。

参考命令形态（实际值随视频变化）：

```bash
taskset -c <本路 CPU 列表> "$INSTALL_DIR/bin/x264" \
  --preset faster --input-res 1920x1080 --fps 60 --frames 9999 \
  --keyint 120 --min-keyint 60 --bframes 0 \
  --bitrate 2000 --vbv-maxrate 2000 --vbv-bufsize 2000 \
  --rc-lookahead 1 --lookahead-threads 3 \
  -o <本次任务的私有码流路径> /home/runner/software/x264/video/Dota2_1920x1080_60.yuv
```

报告指标取原脚本生成的 `video_fps_summary_x264.csv` 中的 `Total FPS`，单位 `fps`，越高越好；按码率分组。结果转换程序只校验原始日志与 CSV，并生成 `benchmark_x264.json` 供框架读取，不执行编码。原始日志合并到 `benchmark_encode.txt`；码流文件留在本次私有工作目录，独立运行结束时由现有清理流程处理。

独立运行：

```bash
bash software/Media/x264/x264_test.sh --version 0.165.3223 \
  --results-dir /home/runner/x264-results
```
