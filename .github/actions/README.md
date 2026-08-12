# GitHub Actions buildctl Composite Actions

替换 `docker/*` 官方 action，底层使用 `buildctl` 连接远程 buildkitd server。

## 文件结构

```text
.github/actions/
├── build-push-action/         # 替换 docker/build-push-action@v7
│   ├── action.yml
│   └── buildctl-build.sh
├── docker-login-action/       # 替换 docker/login-action@v4
│   └── action.yml
├── docker-setup-buildx-action/ # 替换 docker/setup-buildx-action@v4 (no-op)
│   └── action.yml
└── docker-setup-qemu-action/  # 替换 docker/setup-qemu-action@v4 (no-op)
    └── action.yml
```

## 使用方式

用户只需改 `uses` 一行，其他 `with` 参数完全不变：

```yaml
# 之前
- uses: docker/login-action@v4
- uses: docker/setup-buildx-action@v4
- uses: docker/build-push-action@v7

# 之后（用当前 repo）
- uses: ./.github/actions/docker-login-action
- uses: ./.github/actions/docker-setup-buildx-action
- uses: ./.github/actions/build-push-action
```

## 完整对比（以 vllm-ascend 镜像构建为例）

```yaml
# ── 之前：docker/build-push-action@v7 ──
- uses: docker/login-action@v4
  with:
    registry: quay.io
    username: ${{ secrets.QUAY_USERNAME }}
    password: ${{ secrets.QUAY_PASSWORD }}

- uses: docker/setup-buildx-action@v4

- uses: docker/build-push-action@v7
  with:
    context: .
    file: Dockerfile
    push: true
    tags: quay.io/ascend/vllm-ascend:nightly
    build-args: |
      VLLM_COMMIT=abc123
    cache-from: type=registry,ref=ghcr.io/org/repo:buildcache
    cache-to: type=registry,ref=ghcr.io/org/repo:buildcache,mode=max

# ── 之后：只需改 uses（with 全部不变）──
- uses: ./.github/actions/docker-login-action
  with:
    registry: quay.io
    username: ${{ secrets.QUAY_USERNAME }}
    password: ${{ secrets.QUAY_PASSWORD }}

- uses: ./.github/actions/docker-setup-buildx-action

- uses: ./.github/actions/build-push-action
  with:
    context: .
    file: Dockerfile
    push: true
    tags: quay.io/ascend/vllm-ascend:nightly
    build-args: |
      VLLM_COMMIT=abc123
    cache-from: type=registry,ref=ghcr.io/org/repo:buildcache
    cache-to: type=registry,ref=ghcr.io/org/repo:buildcache,mode=max
```

## 注意事项

1. **platforms 不需要设置**——builkitd 只构建本机架构（amd64 / arm64）

2. **cache-to 使用 inline**——内部始终用 `--export-cache type=inline`，`mode=max` 会提示但不生效

3. **setup-buildx 和 setup-qemu** 是 no-op，保留以兼容原有 workflow 结构

4. **Runner 需要**：`BUILDKITD_ADDR` 和 `${DOCKER_CONFIG}/ca.pem,cert.pem,key.pem`（由 Runner Pod 注入）
