#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.15.2}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
SONIC_GO_SOURCE_URL="${SONIC_GO_SOURCE_URL:-https://github.com/bytedance/sonic.git}"
SONIC_GO_PROXY="${SONIC_GO_PROXY:-https://repo.huaweicloud.com/repository/goproxy/|https://goproxy.cn}"
GO_SRC_VERSION="${GO_SRC_VERSION:-1.24.6}"
GO_SOURCE_URL="${GO_SOURCE_URL:-https://gitcode.com/openeuler/golang.git}"
GO_SRC_BRANCH="go${GO_SRC_VERSION}"
TOOLCHAIN_TARGET="${TOOLCHAIN_TARGET:-go${GO_SRC_VERSION}-default}"
GO_BOOTSTRAP_VERSION="${GO_BOOTSTRAP_VERSION:-1.27.0}"
GO_RELEASE_URL="${GO_RELEASE_URL:-https://go.dev/dl}"
GO_OFFLINE_DIR="${GO_OFFLINE_DIR:-/home/runner/software/golang}"

SOURCE_DIR=""
OBJECTS_DIR=""
TOOLCHAIN_DIR=""
GO_BIN=""
GO_ARCH=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[sonic-go] %s\n' "$*"
}

initialize_runtime() {
    local actual_arch expected_arch

    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64)
            expected_arch="x86_64"
            GO_ARCH="amd64"
            ;;
        aarch64|arm64)
            expected_arch="aarch64"
            GO_ARCH="arm64"
            ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) actual_arch="x86_64" ;;
        aarch64|arm64) actual_arch="aarch64" ;;
        *) actual_arch="$(uname -m)" ;;
    esac
    if [[ "${actual_arch}" != "${expected_arch}" ]]; then
        log "ERROR: expected architecture ${expected_arch}, runner is ${actual_arch}"
        return 20
    fi
    EXPECTED_ARCH="${expected_arch}"

    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    if [[ ! "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]]; then
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    fi
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/sonic-go/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi

    OBJECTS_DIR="${PERF_WORK_DIR}/objects"
    SOURCE_DIR="${OBJECTS_DIR}/go/src/sonic"
    TOOLCHAIN_DIR="${OBJECTS_DIR}/go/bin/${TOOLCHAIN_TARGET}"
    GO_BIN="${TOOLCHAIN_DIR}/bin/go"
    TMPDIR="${PERF_WORK_DIR}/tmp"

    # Go 环境隔离：模块/GOPATH/临时目录全部落在 PERF_WORK_DIR；GOTOOLCHAIN=local
    # 禁止 go.mod 触发工具链自动下载；GOENV=off 屏蔽宿主机 go env 文件残留。
    # 显式清掉 GOROOT/GOWORK/GOFLAGS 的全局残留：
    #   GOROOT  残留会让自编译工具链错用宿主机 pkg/tool（版本错配）；
    #   GOWORK  残留 off 会与 sonic 自带 go.work 的 replace 冲突；
    #   GOFLAGS 残留（如 -mod=vendor）会破坏 sonic 的模块解析。
    unset GOROOT GOWORK GOFLAGS GOCACHE
    export GOMODCACHE="${PERF_WORK_DIR}/runtime/module-cache"
    export GOPATH="${PERF_WORK_DIR}/runtime/path"
    export GOPROXY="${SONIC_GO_PROXY}"
    export GOTOOLCHAIN=local
    export GOENV=off
    export SOFTWARE_VERSION EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR
    export PERF_ACTUAL_VERSION_FILE TMPDIR
    export SONIC_GO_BIN="${GO_BIN}" SONIC_GO_VERSION="${GO_SRC_VERSION}"
    export SONIC_GO_TOOLCHAIN="${TOOLCHAIN_TARGET}"
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" "${GOMODCACHE}" "${GOPATH}"
}

run_as_root() {
    if [[ "$1" == dnf && -n "${PERF_PROXY:-}" ]]; then
        set -- "$1" "--setopt=proxy=${PERF_PROXY}" "${@:2}"
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
    elif command -v sudo >/dev/null 2>&1; then
        sudo -n "$@"
    else
        log "ERROR: root privileges are required to install missing dependencies"
        return 30
    fi
}

install_dependencies() {
    local required package
    local packages=()

    for required in git curl gcc tar gzip sha256sum awk python3 taskset; do
        if command -v "${required}" >/dev/null 2>&1; then
            continue
        fi
        case "${required}" in
            git) package="git" ;;
            curl) package="curl" ;;
            gcc) package="gcc" ;;
            tar) package="tar" ;;
            gzip) package="gzip" ;;
            sha256sum|taskset) package="util-linux" ;;
            awk) package="gawk" ;;
            tee) package="coreutils" ;;
            python3) package="python3" ;;
        esac
        packages+=("${package}")
    done
    if [[ "${#packages[@]}" -gt 0 ]]; then
        log "installing missing Sonic Go dependencies: ${packages[*]}"
        if ! command -v dnf >/dev/null 2>&1; then
            log "ERROR: dnf is required to install Sonic Go dependencies"
            return 30
        fi
        run_as_root dnf install -y "${packages[@]}" || return 30
    fi
    for required in git curl gcc tar gzip sha256sum awk python3 taskset; do
        if ! command -v "${required}" >/dev/null 2>&1; then
            log "ERROR: required command remains unavailable: ${required}"
            return 30
        fi
    done
}

copy_or_download_bootstrap_archive() {
    local archive="$1" filename expected actual offline_archive

    filename="$(basename "${archive}")"
    offline_archive="${GO_OFFLINE_DIR}/${filename}"
    case "${GO_BOOTSTRAP_VERSION}:${GO_ARCH}" in
        1.26.7:amd64)
            expected="ffb5f8de10c62550dfddab66b36b57030721e0a44a3218e9e1181d7b59f121ca"
            ;;
        1.26.7:arm64)
            expected="5a4ec883379d51ee9ce1040d5e87f8d35e20387574dd8c947feb01eabc3c1b37"
            ;;
        1.27.0:amd64)
            expected="675c26c449cbb18fc24b74650de1eabbae6e16f64326fd85a283fb3b58280685"
            ;;
        1.27.0:arm64)
            expected="51798d2c42d0e1c6ed7fd9f48728b4193abac9e8aad6dbac2fe96a81f5909bda"
            ;;
        *)
            log "ERROR: no verified Go binary release is declared for bootstrap ${GO_BOOTSTRAP_VERSION} on ${GO_ARCH}"
            return 10
            ;;
    esac
    if [[ -f "${offline_archive}" ]]; then
        log "using offline Go bootstrap archive ${offline_archive}"
        cp "${offline_archive}" "${archive}"
    else
        log "downloading official Go bootstrap archive ${filename}"
        if ! curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
            "${GO_RELEASE_URL}/${filename}"; then
            log "ERROR: failed to download ${filename}"
            return 30
        fi
    fi
    actual="$(sha256sum "${archive}" | awk '{print $1}')"
    if [[ "${actual}" != "${expected}" ]]; then
        log "ERROR: Go bootstrap archive checksum mismatch: ${filename}"
        return 30
    fi
}

# 编译 Go 源码需要 PATH 上有 bootstrap Go（make.bash 自举）。优先使用 Runner
# 已有的 PATH Go；缺失时退回安装任务私有的官方二进制（离线目录优先，校验
# SHA-256 后解压），避免依赖系统包。
resolve_bootstrap_go() {
    if command -v go >/dev/null 2>&1; then
        log "using bootstrap Go from PATH: $(go version 2>/dev/null || echo unknown)"
        return 0
    fi
    local install_dir="${PERF_WORK_DIR}/bootstrap-go"
    local archive
    if [[ -e "${install_dir}" ]]; then
        log "ERROR: bootstrap Go directory already exists: ${install_dir}"
        return 20
    fi
    archive="${PERF_WORK_DIR}/go${GO_BOOTSTRAP_VERSION}.linux-${GO_ARCH}.tar.gz"
    copy_or_download_bootstrap_archive "${archive}" || return $?
    if ! mkdir -p "${install_dir}" || \
       ! tar -xzf "${archive}" -C "${install_dir}" --strip-components=1; then
        log "ERROR: failed to install bootstrap Go ${GO_BOOTSTRAP_VERSION}"
        return 30
    fi
    rm -f "${archive}"
    if [[ ! -x "${install_dir}/bin/go" ]]; then
        log "ERROR: bootstrap Go binary is unavailable: ${install_dir}/bin/go"
        return 30
    fi
    export PATH="${install_dir}/bin:${PATH}"
    log "installed private bootstrap Go ${GO_BOOTSTRAP_VERSION} for ${GO_ARCH}"
}

verify_go_toolchain() {
    local actual_version

    if [[ ! -x "${GO_BIN}" ]]; then
        log "ERROR: built Go toolchain is unavailable: ${GO_BIN}"
        return 40
    fi
    actual_version="$("${GO_BIN}" version | awk '{print $3}' | sed 's/^go//')" || return 40
    if [[ "${actual_version}" != "${GO_SRC_VERSION}" ]]; then
        log "ERROR: built Go is ${actual_version}, expected ${GO_SRC_VERSION}"
        return 40
    fi
}

build_sonic_go() {
    local actual_version go_src_commit

    initialize_runtime || return $?
    install_dependencies || return $?
    resolve_bootstrap_go || return $?
    if [[ -e "${OBJECTS_DIR}/go/src/go${GO_SRC_VERSION}" || -e "${TOOLCHAIN_DIR}" || -e "${SOURCE_DIR}" ]]; then
        log "ERROR: source or toolchain directory already exists under ${PERF_WORK_DIR}"
        return 20
    fi
    mkdir -p "${OBJECTS_DIR}/go/src" "${OBJECTS_DIR}/go/bin"

    export GIT_TERMINAL_PROMPT=0
    log "cloning Go ${GO_SRC_VERSION} source from ${GO_SOURCE_URL} (branch ${GO_SRC_BRANCH})"
    if ! git clone -b "${GO_SRC_BRANCH}" --depth 1 \
        "${GO_SOURCE_URL}" "${OBJECTS_DIR}/go/src/go${GO_SRC_VERSION}"; then
        log "ERROR: failed to clone Go source branch ${GO_SRC_BRANCH}"
        return 30
    fi
    go_src_commit="$(git -C "${OBJECTS_DIR}/go/src/go${GO_SRC_VERSION}" rev-parse HEAD 2>/dev/null || true)"
    log "Go source commit: ${go_src_commit:-unknown}"

    # 一份源码树 = 一个工具链：make.bash 就地生成 bin/go，GOROOT 指向工作副本。
    log "building Go ${GO_SRC_VERSION} toolchain (target: ${TOOLCHAIN_TARGET})"
    if ! cp -a "${OBJECTS_DIR}/go/src/go${GO_SRC_VERSION}" "${TOOLCHAIN_DIR}"; then
        log "ERROR: failed to create toolchain working copy: ${TOOLCHAIN_DIR}"
        return 30
    fi
    # bootstrap 阶段的构建缓存隔离在 runtime/ 下（stop 阶段清理）；测试阶段的
    # GOCACHE 由 sonic_bench.sh 的 setup_gocache 指到工具链目录内，两不混用。
    export GOCACHE="${PERF_WORK_DIR}/runtime/bootstrap-go-build"
    mkdir -p "${GOCACHE}"
    if ! (cd "${TOOLCHAIN_DIR}" && bash "${SCRIPT_DIR}/scripts/go/build_go.sh" default); then
        log "ERROR: failed to build Go ${GO_SRC_VERSION} toolchain"
        return 30
    fi
    unset GOCACHE
    verify_go_toolchain || return $?
    if [[ ! -f "${TOOLCHAIN_DIR}/toolchain_meta.json" ]]; then
        log "ERROR: toolchain metadata is missing: ${TOOLCHAIN_DIR}/toolchain_meta.json"
        return 40
    fi
    log "built private Go ${GO_SRC_VERSION} toolchain at ${TOOLCHAIN_DIR}"

    log "cloning Sonic ${SOFTWARE_VERSION} from ${SONIC_GO_SOURCE_URL}"
    if ! git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${SONIC_GO_SOURCE_URL}" "${SOURCE_DIR}"; then
        log "ERROR: failed to clone Sonic v${SOFTWARE_VERSION}"
        return 30
    fi
    actual_version="$(git -C "${SOURCE_DIR}" describe --tags --exact-match 2>/dev/null || true)"
    if [[ "${actual_version}" != "v${SOFTWARE_VERSION}" ]]; then
        log "ERROR: checked-out Sonic tag ${actual_version:-unknown} does not match v${SOFTWARE_VERSION}"
        return 40
    fi
    log "downloading Sonic's declared Go module dependencies"
    if ! (
        cd "${SOURCE_DIR}"
        "${GO_BIN}" mod download
    ); then
        log "ERROR: failed to prepare the Sonic benchmark packages"
        return 40
    fi
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}"
    log "Sonic ${SOFTWARE_VERSION} benchmark packages are ready"
}

start_sonic_go_runtime() {
    initialize_runtime || return $?
    verify_go_toolchain || return $?
    if [[ ! -f "${TOOLCHAIN_DIR}/toolchain_meta.json" ]]; then
        log "ERROR: toolchain metadata is unavailable: ${TOOLCHAIN_DIR}/toolchain_meta.json"
        return 40
    fi
    if [[ ! -f "${SOURCE_DIR}/go.mod" || ! -d "${SOURCE_DIR}/internal/native" ]]; then
        log "ERROR: Sonic source tree is unavailable: ${SOURCE_DIR}"
        return 40
    fi
    if [[ ! -f "${SCRIPT_DIR}/scripts/go/sonic_bench.sh" ]]; then
        log "ERROR: sonic benchmark script is unavailable"
        return 40
    fi
    log "Sonic benchmark runtime is ready (toolchain: ${TOOLCHAIN_TARGET}, go${GO_SRC_VERSION})"
}

run_sonic_go_benchmarks() {
    local raw_result

    initialize_runtime || return $?
    verify_go_toolchain || return $?
    if [[ ! -d "${SOURCE_DIR}" ]]; then
        log "ERROR: Sonic source directory is unavailable: ${SOURCE_DIR}"
        return 40
    fi

    raw_result="${RESULTS_DIR}/result-sonic-${TOOLCHAIN_TARGET}-base.json"
    log "running the sonic benchmark matrix with toolchain ${TOOLCHAIN_TARGET}"
    # sonic_bench.sh 自身退出码恒为 0；每个模式的真实成败记录在结果 JSON 的
    # runs[].exit_code / 顶层 exit_code 里，由 parse_sonic_bench.py 严格校验，
    # 任一模式失败都让本阶段返回非零。
    if ! (
        export WORKDIR="${PERF_WORK_DIR}"
        export OBJECTS="${OBJECTS_DIR}"
        export RESULTS="${RESULTS_DIR}"
        export SONIC_DIR="${SOURCE_DIR}"
        unset GOCACHE
        bash "${SCRIPT_DIR}/scripts/go/sonic_bench.sh" \
            --toolchain "${TOOLCHAIN_TARGET}" \
            --result "${raw_result}"
    ) 2>&1 | tee "${RESULTS_DIR}/benchmark_sonic_go.txt"; then
        log "ERROR: sonic benchmark run failed"
        return 50
    fi
    if [[ ! -s "${raw_result}" ]]; then
        log "ERROR: sonic benchmark produced no result JSON: ${raw_result}"
        return 50
    fi

    export SOFTWARE_VERSION EXPECTED_ARCH
    if ! python3 "${SCRIPT_DIR}/scripts/parse_sonic_bench.py" \
        "${raw_result}" \
        "${RESULTS_DIR}/benchmark_sonic_go.json"; then
        log "ERROR: failed to normalize sonic benchmark results"
        return 50
    fi
    log "Sonic benchmark results written to benchmark_sonic_go.txt and benchmark_sonic_go.json"
}

stop_sonic_go_runtime() {
    local runtime_dir="${PERF_WORK_DIR}/runtime"

    initialize_runtime || return $?
    if [[ ! -d "${runtime_dir}" ]]; then
        log "Sonic benchmark has no runtime directory to stop"
        return
    fi
    if [[ "${runtime_dir}" != "${PERF_WORK_DIR}/runtime" ]]; then
        log "ERROR: refusing to clean unexpected Sonic Go runtime directory: ${runtime_dir}"
        return 70
    fi
    if ! chmod -R u+w "${runtime_dir}" || ! rm -rf -- "${runtime_dir}"; then
        log "ERROR: failed to remove private Sonic Go runtime directory"
        return 70
    fi
    log "removed private Sonic Go runtime directory: ${runtime_dir}"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping standalone work directory: ${PERF_WORK_DIR}"
        return
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log "external work directory was not removed: ${PERF_WORK_DIR}"
        return
    fi
    if [[ "${PERF_WORK_DIR}" != /home/runner/boostkit-perf/sonic-go/local-* || \
          "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/sonic-go ]]; then
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    fi
    rm -rf -- "${PERF_WORK_DIR}"
}

run_sonic_go_standalone() {
    local stage_status=0 failed_stage="" cleanup_status="passed"

    initialize_runtime || return $?
    trap 'stop_sonic_go_runtime || :; cleanup_standalone_workdir || :' EXIT
    standalone_runtime system "${RESULTS_DIR}/system_info.json" || {
        stage_status=$?
        failed_stage="prepare"
    }
    if [[ "${stage_status}" -eq 0 ]]; then
        standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json" || {
            stage_status=$?
            failed_stage="prepare"
        }
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if build_sonic_go; then
            :
        else
            stage_status=$?
            failed_stage="build"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_sonic_go_runtime; then
            :
        else
            stage_status=$?
            failed_stage="start"
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_sonic_go_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage="test"
        fi
    fi
    if stop_sonic_go_runtime; then
        :
    else
        cleanup_status="failed"
        [[ "${stage_status}" -ne 0 ]] || stage_status=$?
        [[ -n "${failed_stage}" ]] || failed_stage="stop"
    fi
    STANDALONE_STOP_DONE=1
    if standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"; then
        :
    else
        [[ "${stage_status}" -ne 0 ]] || stage_status=$?
        [[ -n "${failed_stage}" ]] || failed_stage="finalize"
    fi
    standalone_runtime finalize \
        "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
        "${stage_status}" "${failed_stage}" "${cleanup_status}" || return $?
    return "${stage_status}"
}

usage() {
    cat <<'USAGE'
Usage: sonic_go_test.sh [OPTIONS]

Build the Go toolchain from source and run the Sonic Go benchmark matrix
(encoder/decoder x 6 runtime modes + parser x 3 runtime modes) as a
standalone evaluation.

Options:
  --version VERSION       Sonic version (default: 1.15.2)
  --results-dir DIRECTORY Persistent result directory
  --keep-workdir          Keep the standalone temporary work directory
  --help                  Show this help text
USAGE
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version) SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir) RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            --help) usage; return 0 ;;
            *) log "ERROR: unknown option: $1"; usage >&2; return 10 ;;
        esac
    done
    run_sonic_go_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
