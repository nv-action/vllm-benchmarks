#!/usr/bin/env bash
# =============================================================================
# build_go.sh —— Go 工具链构建脚本（stage3-build/scripts/go/，由 build.sh 驱动）
#
# 为什么"一份源码树 = 一个工具链"：make.bash 在源码树内就地生成 bin/go，该工具链
# 的 GOROOT 指向这份树（std 源码就在树里），只拷 bin/go 出来无法使用。所以
# build.sh 为每个 target（tc-default / tc-features）在 bin/<target> 下各建一份
# 工作副本（git worktree 或 cp），本脚本在副本内执行 make.bash。
#
# 调用约定（由 build.sh 统一驱动，也可手动等价调用）：
#   cwd   = 工作副本目录（即未来的 GOROOT，通常 $OBJECTS/go/bin/<target>）
#   参数1 = default | features
#     default ：直接 ./make.bash
#     features：先注入特性开关再 ./make.bash，开关经环境变量传入（来自
#               build_object_go.yaml 的 env 段）：
#                 FEATURE_GOEXPERIMENT  逗号分隔的 GOEXPERIMENT 集合
#                 FEATURE_BUILD_ENV 其它编译期环境，"K=V K2=V2" 原样 export
#                 FEATURE_GCFLAGS   值含空格，export 成 GO_GCFLAGS 注入
#                 FEATURE_LDFLAGS   同上，export 成 GO_LDFLAGS 注入
#
# 产物（都在当前工作副本内）：
#   bin/go                编译出的 Go 工具链
#   toolchain_meta.json   版本/commit/开关/时间 元数据（报告回溯锚点）
#   features_used.txt     features 模式实际用到的开关
#
# 前置：目标机已有可用的 Go 可执行程序作 bootstrap（stage1 探测确认）。
# =============================================================================
set -euo pipefail

# 工作副本 = 当前目录（build.sh 已 cd 进来）；取绝对路径防相对路径歧义。
SRC_DIR="$(pwd)"
MODE="${1:?usage: build_go.sh <default|features>}"
[[ "$MODE" == "default" || "$MODE" == "features" ]] || {
  echo "MODE must be 'default' or 'features', got: $MODE" >&2; exit 1; }

# 前置校验：必须是 Go 源码树（存在 src/make.bash）。
[[ -f "$SRC_DIR/src/make.bash" ]] || {
  echo "not a Go source tree (missing src/make.bash): $SRC_DIR" >&2; exit 1; }

# 前置校验：PATH 上必须有 bootstrap Go（make.bash 靠它自举编译）。
command -v go >/dev/null 2>&1 || {
  echo "no bootstrap Go found on PATH; run stage1 prepare_go_env.sh first" >&2; exit 1; }

# 记录元数据（时间戳/commit/分支/是否有未提交改动）。
BEGIN_TS="$(date -u +%Y-%m-%dT%H:%M:%SZ 2>/dev/null || echo unknown)"
GO_SRC_COMMIT="$(git -C "$SRC_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
GO_SRC_BRANCH="$(git -C "$SRC_DIR" rev-parse --abbrev-ref HEAD 2>/dev/null || echo unknown)"
GO_SRC_DIRTY="no"
if git -C "$SRC_DIR" status --porcelain 2>/dev/null | grep -q .; then GO_SRC_DIRTY="yes"; fi

# 收集要传给 make.bash 的额外环境变量（K=V 数组）。
FEATURES_ENV_ARGS=()
if [[ "$MODE" == "features" ]]; then
  # GOEXPERIMENT 集合 + 其它 K=V 编译期环境（GOARM64 等），并记入 features_used.txt。
  if [[ -n "${FEATURE_GOEXPERIMENT:-}" ]]; then
    FEATURES_ENV_ARGS+=(GOEXPERIMENT="$FEATURE_GOEXPERIMENT")
    printf '%s\n' "GOEXPERIMENT=$FEATURE_GOEXPERIMENT" > "$SRC_DIR/features_used.txt"
  fi
  if [[ -n "${FEATURE_BUILD_ENV:-}" ]]; then
    read -r -a kv_pairs <<< "$FEATURE_BUILD_ENV"
    for kv in "${kv_pairs[@]}"; do
      FEATURES_ENV_ARGS+=("$kv")
    done
    printf '%s\n' "$FEATURE_BUILD_ENV" >> "$SRC_DIR/features_used.txt"
  fi
  # gcflags/ldflags 类（值含空格）：export 成 GO_GCFLAGS/GO_LDFLAGS，dist 会以
  # -gcflags=all=... / -ldflags=all=... 作用到 std+cmd 自身。
  #
  # 自举工具链（FEATURE_BOOTSTRAP=<target>）：fork 的自定义开关（如 GOARM64
  # 自定义后缀、私有 GOEXPERIMENT）无法通过官方 bootstrap 的校验——make.bash
  # 第一步就用 bootstrap go 编译 cmd/dist。指定本流水线已构建的 fork 工具链
  # （$OBJECTS/go/bin/<target>）作 GOROOT_BOOTSTRAP 即可绕开：fork 自产的
  # cmd/go 认识这些开关。要求 <target> 已先构建成功。
  if [[ -n "${FEATURE_BOOTSTRAP:-}" ]]; then
    BIN_ROOT="$(dirname "$SRC_DIR")"          # cwd = $OBJECTS/go/bin/<target>
    export GOROOT_BOOTSTRAP="$BIN_ROOT/$FEATURE_BOOTSTRAP"
    if [[ ! -x "$GOROOT_BOOTSTRAP/bin/go" ]]; then
      echo "FEATURE_BOOTSTRAP=$FEATURE_BOOTSTRAP: $GOROOT_BOOTSTRAP/bin/go missing (build it first)" >&2
      exit 1
    fi
    printf '%s\n' "GOROOT_BOOTSTRAP=$GOROOT_BOOTSTRAP" >> "$SRC_DIR/features_used.txt"
    echo "[build_go] bootstrap toolchain: $GOROOT_BOOTSTRAP" >&2
  fi
  if [[ -n "${FEATURE_GCFLAGS:-}" ]]; then
    FEATURES_ENV_ARGS+=(GO_GCFLAGS="$FEATURE_GCFLAGS")
    printf '%s\n' "GO_GCFLAGS=$FEATURE_GCFLAGS" >> "$SRC_DIR/features_used.txt"
  fi
  if [[ -n "${FEATURE_LDFLAGS:-}" ]]; then
    FEATURES_ENV_ARGS+=(GO_LDFLAGS="$FEATURE_LDFLAGS")
    printf '%s\n' "GO_LDFLAGS=$FEATURE_LDFLAGS" >> "$SRC_DIR/features_used.txt"
  fi
  echo "[build_go] mode=features goexperiment: ${FEATURE_GOEXPERIMENT:-<none>} env: ${FEATURE_BUILD_ENV:-<none>} gcflags: ${FEATURE_GCFLAGS:-<none>} ldflags: ${FEATURE_LDFLAGS:-<none>}" >&2
else
  echo "[build_go] mode=default (no extra feature switches)" >&2
fi

# ---- 编译（官方标准流程：cd src && ./make.bash）----
echo "[build_go] building Go from $SRC_DIR (mode=$MODE) ..." >&2
(
  cd "$SRC_DIR/src"
  if [[ ${#FEATURES_ENV_ARGS[@]} -gt 0 ]]; then
    env "${FEATURES_ENV_ARGS[@]}" ./make.bash
  else
    ./make.bash
  fi
)

# 产物校验：make.bash 结束后必须存在可执行的 bin/go。
[[ -x "$SRC_DIR/bin/go" ]] || { echo "make.bash finished but $SRC_DIR/bin/go missing" >&2; exit 1; }

GO_VERSION_OUT="$("$SRC_DIR/bin/go" version 2>/dev/null || echo "version check failed")"

# 写 toolchain_meta.json：版本/commit/分支/开关集合/构建时间/二进制路径。
cat > "$SRC_DIR/toolchain_meta.json" <<EOF
{
  "toolchain_id": "tc-$MODE",
  "mode": "$MODE",
  "go_version": "$(printf '%s' "$GO_VERSION_OUT" | sed 's/"/\\"/g')",
  "go_src_commit": "$GO_SRC_COMMIT",
  "go_src_branch": "$GO_SRC_BRANCH",
  "go_src_dirty": "$GO_SRC_DIRTY",
  "features_goexperiment": "${FEATURE_GOEXPERIMENT:-}",
  "features_build_env": "${FEATURE_BUILD_ENV:-}",
  "features_gcflags": "${FEATURE_GCFLAGS:-}",
  "features_ldflags": "${FEATURE_LDFLAGS:-}",
  "built_at": "$BEGIN_TS",
  "bin_path": "$SRC_DIR/bin/go"
}
EOF

echo "-------------------------------------------------------------------" >&2
echo "built toolchain: $SRC_DIR/bin/go   (GOROOT = $SRC_DIR)" >&2
echo "$GO_VERSION_OUT" >&2
echo "meta: $SRC_DIR/toolchain_meta.json" >&2
echo "-------------------------------------------------------------------" >&2

# ---- 冒烟：编译并运行一个小程序验证工具链可用 ----
# Go 1.20+ 需要 go.mod 才能 go build；用 fmt.Println（写 stdout）而非内建 println
# （写 stderr），否则 2>/dev/null | grep 会永远判定失败。
TMP_SMOKE="$(mktemp -d)"
printf 'module smoke\n\ngo 1.18\n' > "$TMP_SMOKE/go.mod"
printf 'package main\nimport "fmt"\nfunc main(){fmt.Println("ok")}\n' > "$TMP_SMOKE/main.go"
if ( cd "$TMP_SMOKE" && "$SRC_DIR/bin/go" build -o smoke . ) 2>/dev/null && "$TMP_SMOKE/smoke" 2>/dev/null | grep -q ok; then
  echo "[smoke] toolchain OK" >&2
else
  echo "[smoke] WARNING: toolchain produced but smoke test failed" >&2
fi
rm -rf "$TMP_SMOKE"
