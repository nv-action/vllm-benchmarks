# x264 功能测试

功能测试版本使用源码提交 `0480cb05`，与预构建镜像标签一致。Ubuntu 构建使用
GitHub mirror/x264 的固定归档，SHA-256 为
`d0967a1348c85dfde363bb52610403be898171493100561efa0dd05d5fd1ae50`。
在独立目录运行 `configure --enable-shared --enable-pic`，安装到任务私有目录，
通过 `LD_LIBRARY_PATH` 供后续阶段加载动态库。

归档不包含 Git 元数据，CLI 版本为 `0.165.x`。提交标识来自校验后的源码归档，
不从 CLI 数字版本推断提交号。`binary-version.txt` 保留实际版本输出。
prebuilt 模式比较镜像 `/x264` 中的原始源码文件与固定归档，复用
`/x264/x264_build`；未包含源码时，准备固定归档的独立构建目录。
源码校验不等同于预构建二进制的密码学来源证明。

工作流执行 `prepare → install → check → cleanup`。检查项包括：

- `version`：固定源码校验标记和对应的 CLI 版本。
- `upstream-checkasm`：`make checkasm` 后显式执行 `checkasm8`、`checkasm10`。
  Makefile 的 checkasm 目标仅构建测试程序，不会自行运行。
- `encode-smoke`：生成 3 帧 64×64 I420 素材，检查编码成功、码流非空和 NAL 起始码。
- `decoder-dependencies`：按需安装 FFmpeg；安装失败明确判失败。
- `decode-check`：ffprobe 验证 H.264、64×64、3 帧；FFmpeg 严格解码全部码流，
  输出 I420 必须为 18432 字节。

不执行全量软件包升级。APT update 只刷新索引，但安装依赖可能升级相关包，
应检查安装日志中的实际变动。

结果和 checkasm、编码、解码、依赖安装日志写入结果目录；清理删除临时编码数据。
