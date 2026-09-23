# Squid 重写能否替代 index 类 ENV：ASCEND_INDEX_URL / PYTORCH_INDEX_URL 实测分析

> 背景：移除内部镜像源后，评估剩余两个 index 类 build-arg（`ASCEND_INDEX_URL`、
> `PYTORCH_INDEX_URL`）能否也交给 squid 的 `url_rewrite` 处理，从而从 Dockerfile /
> workflow 中彻底去掉。
>
> 方法：先实测索引页的链接形态，再判断重写是否自洽——**重写能否成立，取决于索引页的
> href 形态**。两个 case 的性质不同，不能一概而论。

## 结论速览

| Setting | ENV 能否去掉 | 原因 | squid 能做什么 |
| --- | --- | --- | --- |
| `ASCEND_INDEX_URL` | ⚠️ 能，但需接受 3.2.0 遮蔽 | ascend repo 是唯一的新版来源 | per-package 内容注入规则 |
| `PYTORCH_INDEX_URL` | ❌ 不能 | `+cpu` 只在那个 index，属内容问题 | 只能加速 R2 wheel 下载 |

## 1. ASCEND_INDEX_URL（triton-ascend）——技术上可行，但有遮蔽代价

### 1.1 实测数据

| 源 | 版本覆盖 | 索引页 href 形态 |
| --- | --- | --- |
| `https://pypi.org/simple/triton-ascend/` | 最高 **3.2.0** | PEP 503 |
| `https://mirrors.huaweicloud.com/ascend/repos/pypi/triton-ascend/` | **3.2.1 / 3.2.2**（无 3.2.0） | **文件名相对链接**（`triton_ascend-3.2.2-...whl`） |

### 1.2 重写规则

相对链接形态恰好让重写自洽：

```sh
# 规则：pypi.org 官方 index → ascend repo
https://pypi.org/simple/triton-ascend/*
  echo "OK rewrite-url=\"https://mirrors.huaweicloud.com/ascend/repos/pypi/triton-ascend/${url#*triton-ascend/}\""
```

pip 请求官方 index → 被重写到 ascend repo；页内相对 href 在**客户端可见 URL**（pypi.org）
下解析 → wheel 请求落回同一条规则 → 一条前缀交换规则即覆盖 index + wheel，与规则 1/12
同机制。

### 1.3 代价：这不是镜像重写，而是内容注入

pypi 上真实存在的 **3.2.0 会被遮蔽成 404**（ascend repo 没有该版本）。任何
`pip install triton-ascend==3.2.0` 的作业会挂，且规则位于共享 helper 中，影响全部客户端。

当前只需要 3.2.2。**若确认集群内无人 pin 3.2.0，可以做。**

## 2. PYTORCH_INDEX_URL（+cpu torch）——ENV 不可能移除

原因本质不同：`pypi.org/simple/torch/` 的 torch **就是 CUDA build**，这是「索引内容」问题
而非「索引位置」问题。squid 把 pypi.org 重写到任何镜像，拿到的都是同一份 CUDA 列表；
要 `+cpu` 就必须换 index，而换 index 是客户端语义，重写替代不了。

唯一「去掉 ENV」的路是遮蔽 `pypi.org/simple/torch/*`，那会把全集群 torch 都劫持成 cpu
build（tool-01 的 aarch64 torch 直接炸），绝对不行。

**结论：ENV 保留**（已指向官方 `download.pytorch.org`）。

### 可选提速：download-r2 纯镜像重写

实测 index 页用绝对链接指向 `download-r2.pytorch.org`（R2 域），文件名与 aliyun 平铺目录
一一对应，因此可加一条纯镜像重写：

```sh
# 规则：download-r2.pytorch.org wheel → aliyun 平铺目录
https://download-r2.pytorch.org/whl/cpu/*
  echo "OK rewrite-url=\"https://mirrors.aliyun.com/pytorch-wheels/cpu/${url##*/}\""
```

无遮蔽、可缓存，仅提速 wheel 下载。

## 3. 复现命令

```sh
# 1. ascend repo：triton-ascend 索引页 href 形态
curl -sL --max-time 15 "https://mirrors.huaweicloud.com/ascend/repos/pypi/triton-ascend/" \
  | grep -oE '<a href="[^"]*"' | head -5

# 2. download.pytorch.org /whl/cpu/torch/ 可达性与 href
curl -sL --max-time 15 "https://download.pytorch.org/whl/cpu/torch/" \
  | grep -oE '<a href="[^"]*"' | head -5

# 3. aliyun pytorch-wheels/cpu 页面形态
curl -sL --max-time 15 "https://mirrors.aliyun.com/pytorch-wheels/cpu/" \
  | grep -oE '<a href="[^"]*"' | head -5

# 4. 版本覆盖对比（决定遮蔽风险）
curl -sL --max-time 15 "https://mirrors.huaweicloud.com/ascend/repos/pypi/triton-ascend/" \
  | grep -oE 'triton_ascend-[0-9.]+[^-]*-' | grep -oE '[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.]*' | sort -u | head
curl -sL --max-time 15 "https://pypi.org/simple/triton-ascend/" \
  | grep -oE 'triton_ascend-[0-9.]+' | sort -uV | tail -3

# 5. pypi.org 官方侧 triton-ascend 存在性
curl -sL -o /dev/null -w 'code=%{http_code}\n' --max-time 15 "https://pypi.org/simple/triton-ascend/"
```

## 4. 待决事项

1. **triton-ascend 注入规则是否落地**：需先确认集群内无 `triton-ascend==3.2.0` 的 pin
   （遮蔽风险由使用者拍板）。
2. **download-r2 加速规则**：无遮蔽风险，可直接加入 helper。
3. 两条规则若落地，需在 helper 中实测验证（tool-20 或新增用例）。
