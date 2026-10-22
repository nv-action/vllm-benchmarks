#!/usr/bin/env bash
# =============================================================================
# lib_cpu.sh —— 绑核（CPU binding）公共入口
#
# 本 skill 的**所有**测试脚本统一通过环境变量 CPU_BIND 提供绑核入口：
#   CPU_BIND="0-3"     绑定到核 0~3（taskset -c 0-3）
#   CPU_BIND="0,2,4"   绑定到指定核集合
#   未设置（默认）     绑定**全部可用核**：从 /proc/self/status 的 Cpus_allowed_list
#                     读取进程允许的 CPU 集合（如 "48-95"）并绑定
# 系统有 taskset 时，run_pinned 会用 taskset 包裹被测命令；taskset 不可用
# （如 macOS 原生环境）时退化为直接执行（不绑核）。
#
# 执行测试前应先用 get_available_cores 向用户展示可用核，并询问是否自定义 CPU_BIND；
# 用户不指定时保持 CPU_BIND 未设，脚本默认绑全部可用核。
# =============================================================================
set -euo pipefail

# get_available_cores：输出进程允许的 CPU 集合（如 "48-95" / "0-47"）。
# 优先读 /proc/self/status 的 Cpus_allowed_list（权威）；taskset 可解析输出时兜底；
# 都无法获取时输出空串。
get_available_cores() {
  local cores=""
  if [[ -r /proc/self/status ]]; then
    cores="$(sed -n 's/^Cpus_allowed_list:[[:space:]]*//p' /proc/self/status 2>/dev/null | head -1 || true)"
  fi
  if [[ -z "$cores" ]] && command -v taskset >/dev/null 2>&1; then
    cores="$(taskset -pc $$ 2>/dev/null | sed -n 's/^.*: *//p' || true)"
  fi
  printf '%s' "$cores"
}

# resolve_cpu_bind：返回实际绑核范围。CPU_BIND 未设时默认绑定全部可用核。
resolve_cpu_bind() {
  if [[ -n "${CPU_BIND:-}" ]]; then
    printf '%s' "$CPU_BIND"
  else
    get_available_cores
  fi
}

# run_pinned：绑核执行。taskset 不可用时（如 macOS）警告一次并直接执行，
# 保证在无 taskset 的机器上脚本仍然可用（只是不绑核）。
run_pinned() {
  if command -v taskset >/dev/null 2>&1; then
    local bind
    bind="$(resolve_cpu_bind)"
    if [[ -n "$bind" ]]; then
      taskset -c "$bind" "$@"
    else
      echo "[cpu] WARN: cannot determine available CPU set; running unpinned" >&2
      "$@"
    fi
  else
    if [[ -n "${CPU_BIND:-}" ]]; then
      echo "[cpu] WARN: CPU_BIND=$CPU_BIND set but taskset unavailable; running unpinned" >&2
    fi
    "$@"
  fi
}

# 供结果 JSON 的 env.cpu_bind 字段：实际绑核范围（"taskset:<范围>"），否则 "off"。
cpu_bind_desc() {
  local bind
  bind="$(resolve_cpu_bind)"
  if [[ -n "$bind" ]]; then
    echo "taskset:$bind"
  else
    echo "off"
  fi
}

# -----------------------------------------------------------------------------
# 以下三个函数供「需要给不同角色指定不同核范围」的用例使用（如 etcd 的服务端
# 与压测客户端必须分核，否则两者争抢同一组核，测出的是争抢后的吞吐而非被测
# 对象的真实性能）。既有脚本不受影响——这些是纯新增。
# -----------------------------------------------------------------------------

# run_pinned_on <核范围> <命令...>：把命令绑到**指定**核范围执行（不读 CPU_BIND）。
# 核范围为空时退化为 run_pinned（回到 CPU_BIND / 全部可用核）。
run_pinned_on() {
  local bind="${1:-}"; shift || true
  if [[ -z "$bind" ]]; then
    run_pinned "$@"
    return
  fi
  if command -v taskset >/dev/null 2>&1; then
    taskset -c "$bind" "$@"
  else
    echo "[cpu] WARN: 指定绑核 '$bind' 但 taskset 不可用；未绑核执行" >&2
    "$@"
  fi
}

# bind_desc <核范围>：结果 JSON 用。指定了核范围记 "taskset:<范围>"，否则回退 cpu_bind_desc。
bind_desc() {
  local bind="${1:-}"
  if [[ -n "$bind" ]]; then
    echo "taskset:$bind"
  else
    cpu_bind_desc
  fi
}

# expand_cores <规格>：展开为逐核一行。支持 "48-51" / "0,2,4" / "48-51,60"。
expand_cores() {
  local spec="${1:-}" part a b i
  [[ -n "$spec" ]] || return 0
  local IFS=','
  for part in $spec; do
    if [[ "$part" == *-* ]]; then
      a="${part%-*}"; b="${part#*-}"
      [[ "$a" =~ ^[0-9]+$ && "$b" =~ ^[0-9]+$ ]] || continue
      for ((i = a; i <= b; i++)); do printf '%s\n' "$i"; done
    else
      [[ "$part" =~ ^[0-9]+$ ]] && printf '%s\n' "$part"
    fi
  done
}

# compress_cores <核号...>：把核号列表压成紧凑范围（48 49 50 51 -> "48-51"）。
compress_cores() {
  local -a nums
  mapfile -t nums < <(printf '%s\n' "$@" | sort -n -u)
  local n="${#nums[@]}"
  ((n > 0)) || return 0
  local out="" start="${nums[0]}" prev="${nums[0]}" i v
  for ((i = 1; i < n; i++)); do
    v="${nums[i]}"
    if ((v == prev + 1)); then prev="$v"; continue; fi
    out+="${out:+,}"
    if [[ "$start" == "$prev" ]]; then out+="$start"; else out+="$start-$prev"; fi
    start="$v"; prev="$v"
  done
  out+="${out:+,}"
  if [[ "$start" == "$prev" ]]; then out+="$start"; else out+="$start-$prev"; fi
  printf '%s' "$out"
}
