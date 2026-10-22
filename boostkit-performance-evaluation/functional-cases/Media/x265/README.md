# x265 功能检查

工作流按 `prepare → install → check → cleanup` 执行。`check` 使用现场生成的
3 帧 64×64、8 位 I420 素材，必须全部满足：

- `version`：版本去掉本地后缀后等于请求版本。
- `decoder-dependencies`：存在 `ffmpeg` 和 `ffprobe`。缺失时 openEuler 使用
  `dnf install`、Ubuntu 使用 `apt-get update` 和 `apt-get install` 安装 FFmpeg。
  APT update 仅刷新索引，不执行全量软件包升级；安装仍可能调整相关依赖。
  软件源不提供依赖或安装失败时，此项及整体结果失败，不静默跳过解码。
- `quality-metrics`：最终编码摘要为 3 帧，PSNR 为有限正数，SSIM 为 [0,1]
  内有限数。这是指标有效性校验，未设置画质基线门槛，也不是上游完整测试套件。
- `encode-smoke`：编码命令成功、码流非空且存在 NAL 起始码。
- `decode-check`：ffprobe 读取 HEVC、64×64、3 帧；FFmpeg 严格解码整个码流，
  输出 I420 为 18432 字节，不通过缩放或限制帧数修正输出。

结果和诊断信息保存在结果目录的 `functional_result.json`、`check.log`、
`decoder_dependencies.log`、`quality_metrics.log`、`encode.log`、`probe.json`
和 `decode.log` 中。清理阶段删除临时码流和解码文件，保留上述日志。

验证解析器：

```bash
python3 -B -m unittest discover -s functional-cases/Media/x265 -p 'test_validate_output.py'
```
