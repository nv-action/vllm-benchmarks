#!/usr/bin/env bash
# x264 构建脚本:环境采集 + 准备被测软件(构建或校验)。
#   FUNC_MODE=prebuilt - 校验镜像内 x264 版本,并校验源码树以跑上游 checkasm
#   FUNC_MODE=build    - 固定校验归档源码编译(configure && make)
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(cc --version 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "compiler_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 固定归档与参考 Dockerfile 使用相同的校验值。
fetch_x264_source() {
    [[ "${SOFTWARE_VERSION}" == 0480cb05 ]] || return 10
    [[ ! -e "${SOURCE_DIR}" ]] || { log "ERROR: source directory is not clean"; return 20; }
    local archive="${PERF_WORK_DIR}/x264.tar.gz"
    curl -fSL --connect-timeout 30 --retry 3 -o "${archive}" "${X264_ARCHIVE_URL}" || return 30
    printf '%s  %s\n' "${X264_ARCHIVE_SHA256}" "${archive}" | sha256sum -c - || return 30
    mkdir -p "${SOURCE_DIR}" || return 30
    tar -xzf "${archive}" -C "${SOURCE_DIR}" --strip-components=1 || return 30
    printf '%s\n' "${X264_ARCHIVE_SHA256}" >"${PERF_WORK_DIR}/source-sha256.txt"
}

configure_x264() {
    mkdir -p "${BUILD_DIR}" "${INSTALL_DIR}" || return 40
    (cd "${BUILD_DIR}" && ../configure --enable-shared --enable-pic --prefix="${INSTALL_DIR}") || return 40
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_build_commands || return $?
    fetch_x264_source || return $?

    local tree actual
    if [[ "${FUNC_MODE}" == prebuilt ]]; then
        [[ -x "${X264_BIN}" ]] || return 40
        tree="${X264_SOURCE_DIR:-/x264}"
        if [[ -d "${tree}" ]]; then
            # 只比较归档中原有文件，镜像中的构建产物不参与比较。
            python3 - "${SOURCE_DIR}" "${tree}" <<'SOURCE_CHECK' || return 40
from pathlib import Path
import sys
reference, installed = map(Path, sys.argv[1:])
for path in reference.rglob('*'):
    if path.is_file():
        other = installed / path.relative_to(reference)
        if not other.is_file() or path.read_bytes() != other.read_bytes():
            raise SystemExit(f"image source differs from pinned archive: {other}")
SOURCE_CHECK
            if [[ -f "${tree}/x264_build/config.mak" && -f "${tree}/x264_build/Makefile" ]]; then
                tree="${tree}/x264_build"
            else
                configure_x264 || return $?
                tree="${BUILD_DIR}"
            fi
        else
            configure_x264 || return $?
            tree="${BUILD_DIR}"
        fi
    else
        configure_x264 || return $?
        (cd "${BUILD_DIR}" && make -j"$(nproc)" && make install) || return 40
        tree="${BUILD_DIR}"
    fi
    printf '%s\n' "${tree}" >"${PERF_WORK_DIR}/checkasm-build-dir"
    actual="$(read_x264_version || true)"
    verify_x264_version || { log "ERROR: unexpected x264 version/source: ${actual:-unknown}"; return 40; }
    "${X264_BIN}" --version >"${RESULTS_DIR}/binary-version.txt" 2>&1 || return 40
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "x264 source ${SOFTWARE_VERSION}, CLI ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare x264 for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    x264 version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_build_standalone() {
    local status=0
    initialize_runtime || return $?
    func_prepare || return $?
    func_install || status=$?
    return "${status}"
}

main() {
    parse_common_args "$@"
    case "$?" in
        0) ;;
        64)
            usage
            return 0
            ;;
        *) return 10 ;;
    esac
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    run_build_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi