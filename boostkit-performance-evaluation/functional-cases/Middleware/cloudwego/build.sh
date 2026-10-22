#!/usr/bin/env bash
# CloudWeGo: fixed Dockerfile component tags; build tools privately on Ubuntu,
# validate existing CLI tools and Eino source in prebuilt images.
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
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 安装私有 Go 工具链:优先用 PATH 上的 go,否则从官方 go.dev/dl 取对应架构归档。
install_go_toolchain() {
    resolve_go_toolchain && return 0
    local filename archive
    filename="go${GO_SRC_VERSION}.linux-${GO_ARCH}.tar.gz"
    archive="${PERF_WORK_DIR}/${filename}"
    log "downloading official Go toolchain ${filename}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${GO_RELEASE_URL}/${filename}" || {
        log "ERROR: failed to download Go ${GO_SRC_VERSION} for ${GO_ARCH}"
        return 30
    }
    [[ ! -e "${GO_DIR}" ]] || {
        log "ERROR: Go toolchain directory already exists: ${GO_DIR}"
        return 20
    }
    mkdir -p "${GO_DIR}"
    tar -xzf "${archive}" -C "${GO_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract the Go toolchain"
        return 30
    }
    rm -f "${archive}"
    GO_BIN="${GO_DIR}/bin/go"
    [[ -x "${GO_BIN}" ]] || {
        log "ERROR: Go binary is unavailable after install: ${GO_BIN}"
        return 30
    }
    log "installed private Go toolchain: $("${GO_BIN}" version | awk '{print $3}')"
}

# 使用参考 Dockerfile 的固定 tag 归档，保留嵌套模块（Hertz cmd/hz）。
acquire_component() {
    local name="$1" tag="$2" dest="$3" archive="${PERF_WORK_DIR}/$1.tar.gz"
    [[ ! -e "${dest}" ]] || { log "ERROR: source directory is not clean: ${dest}"; return 20; }
    curl -fSL --connect-timeout 30 --retry 3 -o "${archive}" \
        "${CLOUDWEGO_SOURCE_BASE}/${name}/archive/refs/tags/${tag}.tar.gz" || return 30
    python3 "${SCRIPT_DIR}/verify_source.py" "${name}" "${tag}" "${archive}" || return 30
    mkdir -p "${dest}" || return 30
    tar -xzf "${archive}" -C "${dest}" --strip-components=1 || return 30
    sha256sum "${archive}" >>"${RESULTS_DIR}/source-sha256.txt" || return 30
    printf '%s\n' "${tag}" >"${dest}/.cloudwego-version"
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    [[ "${SOFTWARE_VERSION}" == 0.16.4 ]] || return 10
    require_build_commands || return $?
    install_go_toolchain || return $?
    export PATH="$(dirname "${GO_BIN}"):${PATH}"
    acquire_component kitex "v${SOFTWARE_VERSION}" "${KITEX_SOURCE_DIR}" || return $?
    acquire_component hertz "v${CLOUDWEGO_HERTZ_VERSION}" "${HERTZ_SOURCE_DIR}" || return $?
    acquire_component eino "v${CLOUDWEGO_EINO_VERSION}" "${EINO_SOURCE_DIR}" || return $?
    acquire_component volo "volo-thrift-${CLOUDWEGO_VOLO_VERSION}" "${VOLO_SOURCE_DIR}" || return $?
    if [[ "${FUNC_MODE}" == build ]]; then
        mkdir -p "${INSTALL_DIR}/bin"
        (cd "${KITEX_SOURCE_DIR}" && "${GO_BIN}" build -ldflags="-s -w" -o "${KITEX_BIN}" ./tool/cmd/kitex) || return 40
        (cd "${HERTZ_SOURCE_DIR}/cmd/hz" && "${GO_BIN}" build -ldflags="-s -w" -o "${HZ_BIN}" .) || return 40
        (cd "${VOLO_SOURCE_DIR}" && cargo build --release --locked -p volo-cli) || return 40
        install -m755 "${CARGO_TARGET_DIR}/release/volo" "${VOLO_BIN}" || return 40
    else
        # 镜像删除了 Kitex/Hertz 源码；重新下载仅供上游测试，工具本身必须用镜像已有的。
        [[ -f /eino/go.mod ]] || { log "ERROR: image Eino source missing"; return 40; }
        python3 - "${EINO_SOURCE_DIR}" /eino <<'EINO_CHECK' || return 40
from pathlib import Path
import sys
reference, image = map(Path, sys.argv[1:])
for p in reference.rglob('*'):
    if p.is_file() and p.name != '.cloudwego-version':
        other = image / p.relative_to(reference)
        if not other.is_file() or p.read_bytes() != other.read_bytes():
            raise SystemExit(f'image Eino source mismatch: {other}')
EINO_CHECK
    fi
    (cd "${EINO_SOURCE_DIR}" && "${GO_BIN}" mod download && "${GO_BIN}" build ./...) || return 40
    # 使用 Kitex go.mod 锁定的 thriftgo v0.4.5，避免 hz 自动安装 @latest。
    mkdir -p "${INSTALL_DIR}/bin"
    (cd "${KITEX_SOURCE_DIR}" && "${GO_BIN}" build -o "${INSTALL_DIR}/bin/thriftgo" github.com/cloudwego/thriftgo) || return 40
    verify_components || return $?
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${SOFTWARE_VERSION}" >"${PERF_ACTUAL_VERSION_FILE}"
    log "CloudWeGo ready: Kitex ${SOFTWARE_VERSION}, Hertz ${CLOUDWEGO_HERTZ_VERSION}, Volo tag ${CLOUDWEGO_VOLO_VERSION} (CLI ${CLOUDWEGO_VOLO_CLI_VERSION}), Eino ${CLOUDWEGO_EINO_VERSION}"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare CloudWeGo for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    CloudWeGo (Kitex) version (default: ${SOFTWARE_VERSION})
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
