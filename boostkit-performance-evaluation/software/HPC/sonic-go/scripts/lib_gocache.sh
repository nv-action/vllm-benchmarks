#!/usr/bin/env bash
# =============================================================================
# lib_gocache.sh —— 每工具链独立 GOCACHE（Go 构建缓存隔离）
#
# 背景：Go 的构建缓存默认全局共享（~/.cache/go-build）。多个工具链 / 不同
#   GOARM64/GOEXPERIMENT 配置共用一份缓存时，会出现「缓存对象与当前配置不匹配」
#   的构建失败，典型报错：
#     could not import internal/goarch
#     (object is [GOARM64=...] expected [GOARM64=...])
#   实测（go1.26develop / go1.26.4 fork）：给 GOARM64 增加 fork 自定义后缀（如
#   rprfm）时构建缓存未正确失效，复用了旧配置的 std 对象 → 同一构建内 std 的
#   build ID 不一致 → 编译中断。加 `go build -a`（强制重编）或用全新缓存可恢复。
#
# 对策（本 lib）：**每个工具链 target 一个独立 GOCACHE**，就放在该工具链自己的
#   GOROOT 下 ——
#   $BIN_DIR/<toolchain_target>/.cache/go-build
#   缓存随工具链产物走（stage3 建出的 worktree 目录内），天然按工具链隔离；
#   同一工具链内跨多次运行仍复用缓存（不牺牲重复运行的速度）。
#   调用方已显式设置 GOCACHE 时尊重之。
#   注意：工具链被重建（worktree 重新生成）时缓存随之清空，属预期。
#
# 实测定位（2026-09-11）：污染是**跨工具链**的。同一工具链内不同 GOARM64 配置
#   （如 v9.0 与 v9.0,rprfm）在**干净缓存**里可正常共存；但只要缓存里混有**另一个
#   工具链**的产物，同一序列就会失败（A/B 对照实验：全新缓存 + 仅本工具链 → 成功；
#   同一全新缓存里先跑过 go1.26develop → 1.26.4 的 v9.0,rprfm 即失败）。
#   故按工具链隔离缓存即可消除该问题。
#
# 用法（在解析出 TOOLCHAIN_TARGET 之后调用）：
#   source "$SCRIPT_DIR/../lib_gocache.sh"
#   setup_gocache "$TOOLCHAIN_TARGET"
# =============================================================================
set -u

# setup_gocache <toolchain_target>：把 GOCACHE 指到该工具链 GOROOT 下的 .cache 并建好。
#   优先用调用方已设的 BIN_DIR（脚本里已解析），否则按默认约定推导。
setup_gocache() {
  local target="${1:?setup_gocache: 需要 toolchain target 名}"
  local bin="${BIN_DIR:-${OBJECTS:-${WORKDIR:-$HOME/tmp}/objects}/go/bin}"
  export GOCACHE="${GOCACHE:-$bin/$target/.cache/go-build}"
  mkdir -p "$GOCACHE" 2>/dev/null || true
}

# gocache_desc：输出当前实际使用的 GOCACHE（供结果 JSON / 日志记录）。
gocache_desc() {
  printf '%s' "${GOCACHE:-<默认>}"
}
