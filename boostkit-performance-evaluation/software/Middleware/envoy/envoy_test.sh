#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SOFTWARE_VERSION="${SOFTWARE_VERSION:-1.39.1}"
EXPECTED_ARCH="${EXPECTED_ARCH:-$(uname -m)}"
PERF_RUN_ID="${PERF_RUN_ID:-}"
RESULTS_DIR="${RESULTS_DIR:-}"
PERF_WORK_DIR="${PERF_WORK_DIR:-}"
PERF_ACTUAL_VERSION_FILE="${PERF_ACTUAL_VERSION_FILE:-}"
PERF_GITHUB_DOWNLOAD_PROXY="${PERF_GITHUB_DOWNLOAD_PROXY:-}"

K6_VERSION="2.2.0"
ENVOY_OFFLINE_DIR="${ENVOY_OFFLINE_DIR:-/home/runner/software/envoy}"

INSTALL_DIR=""
ENVOY_BIN=""
K6_BIN=""
RUNTIME_DIR=""
DIRECT_PID_FILE=""
PROXY_PID_FILE=""
BACKEND_PID_FILE=""
STANDALONE_OWNS_WORK_DIR=0
STANDALONE_KEEP_WORK_DIR=0
STANDALONE_STOP_DONE=0
STANDALONE_CLEANUP_DONE=0

log() {
    printf '[envoy] %s\n' "$*"
}

configure_runtime_paths() {
    local actual_arch

    case "${EXPECTED_ARCH,,}" in
        x86_64|amd64)
            EXPECTED_ARCH="x86_64"
            actual_arch="x86_64"
            K6_ARCH="amd64"
            ;;
        aarch64|arm64)
            EXPECTED_ARCH="aarch64"
            actual_arch="aarch64"
            K6_ARCH="arm64"
            ;;
        *)
            log "ERROR: unsupported expected architecture: ${EXPECTED_ARCH}"
            return 20
            ;;
    esac
    case "$(uname -m)" in
        x86_64|amd64) [[ "${actual_arch}" == "x86_64" ]] || actual_arch="x86_64" ;;
        aarch64|arm64) [[ "${actual_arch}" == "aarch64" ]] || actual_arch="aarch64" ;;
        *) actual_arch="$(uname -m)" ;;
    esac
    [[ "${actual_arch}" == "${EXPECTED_ARCH}" ]] || {
        log "ERROR: expected architecture ${EXPECTED_ARCH}, runner is ${actual_arch}"
        return 20
    }

    if [[ -z "${PERF_RUN_ID}" ]]; then
        PERF_RUN_ID="local-$(date -u '+%Y%m%dT%H%M%SZ')-$$"
    fi
    [[ "${PERF_RUN_ID}" =~ ^[A-Za-z0-9._-]+$ ]] || {
        log "ERROR: PERF_RUN_ID contains unsafe characters: ${PERF_RUN_ID}"
        return 10
    }
    if [[ -z "${RESULTS_DIR}" ]]; then
        RESULTS_DIR="${SCRIPT_DIR}/results/${SOFTWARE_VERSION}/${PERF_RUN_ID}"
    fi
    if [[ -z "${PERF_WORK_DIR}" ]]; then
        PERF_WORK_DIR="/home/runner/boostkit-perf/envoy/local-${PERF_RUN_ID}"
        STANDALONE_OWNS_WORK_DIR=1
    fi
    if [[ -z "${PERF_ACTUAL_VERSION_FILE}" ]]; then
        PERF_ACTUAL_VERSION_FILE="${RESULTS_DIR}/actual-version.txt"
    fi
    TMPDIR="${PERF_WORK_DIR}/tmp"

    INSTALL_DIR="${PERF_WORK_DIR}/envoy-install"
    ENVOY_BIN="${INSTALL_DIR}/envoy"
    K6_BIN="${PERF_WORK_DIR}/tools/k6"
    RUNTIME_DIR="${PERF_WORK_DIR}/runtime"
    DIRECT_PID_FILE="${RUNTIME_DIR}/direct.pid"
    PROXY_PID_FILE="${RUNTIME_DIR}/proxy.pid"
    BACKEND_PID_FILE="${RUNTIME_DIR}/backend.pid"
    export EXPECTED_ARCH PERF_RUN_ID RESULTS_DIR PERF_WORK_DIR PERF_ACTUAL_VERSION_FILE TMPDIR
}

initialize_runtime() {
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" "${PERF_WORK_DIR}" "${TMPDIR}" || return 30
}

run_as_root() {
    if [[ "$1" == dnf && -n "${PERF_PROXY:-}" ]]; then
        set -- "$1" "--setopt=proxy=${PERF_PROXY}" "${@:2}"
    fi
    if [[ "${EUID}" -eq 0 ]]; then
        "$@"
        return
    fi
    if ! command -v sudo >/dev/null 2>&1; then
        log "ERROR: root privileges are required to install missing dependencies"
        return 30
    fi
    sudo -n "$@"
}

require_envoy_tools() {
    local command_name package
    local packages=()

    for command_name in curl sha256sum tar gzip install python3 openssl awk sed tee pgrep; do
        if command -v "${command_name}" >/dev/null 2>&1; then
            continue
        fi
        case "${command_name}" in
            curl) package="curl" ;;
            sha256sum|install|tee) package="coreutils" ;;
            tar) package="tar" ;;
            gzip) package="gzip" ;;
            python3) package="python3" ;;
            openssl) package="openssl" ;;
            awk) package="gawk" ;;
            sed) package="sed" ;;
            pgrep) package="procps-ng" ;;
        esac
        log "missing required Envoy command: ${command_name}"
        packages+=("${package}")
    done
    if [[ "${#packages[@]}" -gt 0 ]]; then
        if ! command -v dnf >/dev/null 2>&1; then
            log "ERROR: dnf is required to install Envoy dependencies"
            return 30
        fi
        run_as_root dnf install -y "${packages[@]}" || return 30
    fi

    for command_name in curl sha256sum tar gzip install python3 openssl awk sed tee pgrep; do
        command -v "${command_name}" >/dev/null 2>&1 || {
            log "ERROR: required Envoy command remains unavailable: ${command_name}"
            return 30
        }
    done
}

copy_or_download_verified_file() {
    local url="$1" asset_name="$2" output="$3" expected_sha256="$4"
    local actual_sha256 download_url local_file

    local_file="${ENVOY_OFFLINE_DIR}/${asset_name}"
    if [[ -f "${local_file}" ]]; then
        log "using local release asset ${local_file}"
        install -m 0644 "${local_file}" "${output}" || return 30
    else
        download_url="${url}"
        if [[ -n "${PERF_GITHUB_DOWNLOAD_PROXY}" ]]; then
            download_url="${PERF_GITHUB_DOWNLOAD_PROXY%/}/${url}"
        fi
        log "downloading ${url}"
        curl -fsSL --retry 3 --connect-timeout 30 -o "${output}" "${download_url}" || {
            log "ERROR: download failed: ${url}"
            return 30
        }
    fi
    actual_sha256="$(sha256sum "${output}" | awk '{print $1}')"
    [[ "${actual_sha256}" == "${expected_sha256}" ]] || {
        log "ERROR: checksum mismatch for $(basename "${output}")"
        return 30
    }
}

install_release_artifacts() {
    local envoy_asset envoy_sha256 envoy_url k6_archive k6_dir k6_sha256 k6_url

    case "${EXPECTED_ARCH}" in
        x86_64)
            envoy_asset="envoy-${SOFTWARE_VERSION}-linux-x86_64"
            envoy_sha256="002c6e1c69ed0fa0ea381887247cadadfaec9481375fa8d8d2b1731eeabf40b8"
            k6_sha256="b5a8003c86f35f5cd5ceef1490312c48e587696c94d998cefc6d7b3b4cb1597d"
            ;;
        aarch64)
            envoy_asset="envoy-${SOFTWARE_VERSION}-linux-aarch_64"
            envoy_sha256="8565ad0af4b1d1d3c986e5165c027add3073579182f398dd7f4d728d25e9ec62"
            k6_sha256="4ecd64cadcc792402d16293836115480419c4447c032858f564852d98f1bf54c"
            ;;
    esac

    mkdir -p "${INSTALL_DIR}" "${PERF_WORK_DIR}/tools" || return 30
    envoy_url="https://github.com/envoyproxy/envoy/releases/download/v${SOFTWARE_VERSION}/${envoy_asset}"
    copy_or_download_verified_file \
        "${envoy_url}" "${envoy_asset}" "${ENVOY_BIN}" "${envoy_sha256}" || return $?
    chmod 0755 "${ENVOY_BIN}" || return 30

    k6_archive="${PERF_WORK_DIR}/tools/k6-v${K6_VERSION}-linux-${K6_ARCH}.tar.gz"
    k6_url="https://github.com/grafana/k6/releases/download/v${K6_VERSION}/k6-v${K6_VERSION}-linux-${K6_ARCH}.tar.gz"
    copy_or_download_verified_file \
        "${k6_url}" "$(basename "${k6_archive}")" "${k6_archive}" "${k6_sha256}" || return $?
    k6_dir="${PERF_WORK_DIR}/tools/k6-v${K6_VERSION}-linux-${K6_ARCH}"
    tar -xzf "${k6_archive}" -C "${PERF_WORK_DIR}/tools" || return 30
    install -m 0755 "${k6_dir}/k6" "${K6_BIN}" || return 30
    "${K6_BIN}" version >/dev/null || return 30
}

build_envoy() {
    initialize_runtime || return $?
    require_envoy_tools || return $?
    [[ ! -e "${INSTALL_DIR}" ]] || {
        log "ERROR: Envoy work paths are not clean under ${PERF_WORK_DIR}"
        return 20
    }
    install_release_artifacts || return $?
    "${ENVOY_BIN}" --version | grep -Fq "${SOFTWARE_VERSION}" || {
        log "ERROR: official Envoy release version does not contain ${SOFTWARE_VERSION}"
        return 40
    }
    printf '%s\n' "${SOFTWARE_VERSION}" > "${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "installed official Envoy ${SOFTWARE_VERSION} release into ${INSTALL_DIR}"
}

write_runtime_configurations() {
    mkdir -p "${RUNTIME_DIR}" || return 40
    openssl req -x509 -newkey rsa:2048 -nodes -days 1 \
        -keyout "${RUNTIME_DIR}/tls.key" -out "${RUNTIME_DIR}/tls.crt" \
        -subj '/CN=localhost' >/dev/null 2>&1 || return 40

    cat > "${RUNTIME_DIR}/backend.yaml" <<EOF
static_resources:
  listeners:
  - name: backend
    address:
      socket_address: { address: 127.0.0.1, port_value: 18080 }
    filter_chains:
    - filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: backend
          route_config:
            name: backend_route
            virtual_hosts:
            - name: backend
              domains: ["*"]
              routes:
              - match: { prefix: "/" }
                direct_response: { status: 200, body: { inline_string: "ok" } }
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
admin:
  access_log_path: /dev/null
  address:
    socket_address: { address: 127.0.0.1, port_value: 18081 }
EOF
    cat > "${RUNTIME_DIR}/direct.yaml" <<EOF
static_resources:
  listeners:
  - name: direct_tls
    address:
      socket_address: { address: 127.0.0.1, port_value: 19000 }
    filter_chains:
    - transport_socket:
        name: envoy.transport_sockets.tls
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext
          common_tls_context:
            tls_certificates:
            - certificate_chain: { filename: "${RUNTIME_DIR}/tls.crt" }
              private_key: { filename: "${RUNTIME_DIR}/tls.key" }
      filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: direct
          route_config:
            name: direct_route
            virtual_hosts:
            - name: direct
              domains: ["*"]
              routes:
              - match: { prefix: "/" }
                direct_response: { status: 200, body: { inline_string: "ok" } }
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
admin:
  access_log_path: /dev/null
  address:
    socket_address: { address: 127.0.0.1, port_value: 19002 }
EOF
    cat > "${RUNTIME_DIR}/proxy.yaml" <<EOF
static_resources:
  listeners:
  - name: proxy_tls
    address:
      socket_address: { address: 127.0.0.1, port_value: 19001 }
    filter_chains:
    - transport_socket:
        name: envoy.transport_sockets.tls
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.transport_sockets.tls.v3.DownstreamTlsContext
          common_tls_context:
            tls_certificates:
            - certificate_chain: { filename: "${RUNTIME_DIR}/tls.crt" }
              private_key: { filename: "${RUNTIME_DIR}/tls.key" }
      filters:
      - name: envoy.filters.network.http_connection_manager
        typed_config:
          "@type": type.googleapis.com/envoy.extensions.filters.network.http_connection_manager.v3.HttpConnectionManager
          stat_prefix: proxy
          route_config:
            name: proxy_route
            virtual_hosts:
            - name: proxy
              domains: ["*"]
              routes:
              - match: { prefix: "/" }
                route: { cluster: local_backend }
          http_filters:
          - name: envoy.filters.http.router
            typed_config:
              "@type": type.googleapis.com/envoy.extensions.filters.http.router.v3.Router
  clusters:
  - name: local_backend
    type: STATIC
    connect_timeout: 1s
    load_assignment:
      cluster_name: local_backend
      endpoints:
      - lb_endpoints:
        - endpoint:
            address:
              socket_address: { address: 127.0.0.1, port_value: 18080 }
admin:
  access_log_path: /dev/null
  address:
    socket_address: { address: 127.0.0.1, port_value: 19003 }
EOF
}

start_envoy_process() {
    local name="$1" configuration="$2" pid_file="$3"
    "${ENVOY_BIN}" --config-path "${configuration}" --disable-hot-restart \
        > "${RUNTIME_DIR}/${name}.log" 2>&1 &
    printf '%s\n' "$!" > "${pid_file}"
}

wait_for_https_service() {
    local url="$1" attempts=30
    while (( attempts > 0 )); do
        if curl --silent --show-error --fail --insecure "${url}" >/dev/null; then
            return 0
        fi
        sleep 1
        ((attempts--))
    done
    log "ERROR: Envoy service did not become ready: ${url}"
    return 40
}

start_envoy_runtime() {
    initialize_runtime || return $?
    [[ -x "${ENVOY_BIN}" && -x "${K6_BIN}" ]] || {
        log "ERROR: Envoy build artifacts are unavailable"
        return 40
    }
    [[ ! -e "${RUNTIME_DIR}" ]] || {
        log "ERROR: Envoy runtime directory is not clean: ${RUNTIME_DIR}"
        return 20
    }
    write_runtime_configurations || {
        log "ERROR: failed to generate isolated Envoy runtime configurations"
        return 40
    }
    start_envoy_process backend "${RUNTIME_DIR}/backend.yaml" "${BACKEND_PID_FILE}"
    start_envoy_process direct "${RUNTIME_DIR}/direct.yaml" "${DIRECT_PID_FILE}"
    start_envoy_process proxy "${RUNTIME_DIR}/proxy.yaml" "${PROXY_PID_FILE}"
    wait_for_https_service 'https://127.0.0.1:19000/' || return $?
    wait_for_https_service 'https://127.0.0.1:19001/' || return $?
    log "Envoy direct-response and reverse-proxy scenarios are ready"
}

run_envoy_benchmarks() {
    initialize_runtime || return $?
    [[ -x "${K6_BIN}" && -f "${DIRECT_PID_FILE}" && -f "${PROXY_PID_FILE}" ]] || {
        log "ERROR: Envoy benchmark runtime is unavailable"
        return 50
    }
    python3 "${SCRIPT_DIR}/scripts/run_k6_benchmark.py" \
        --k6 "${K6_BIN}" \
        --direct-url 'https://127.0.0.1:19000/' \
        --reverse-url 'https://127.0.0.1:19001/' \
        --output "${RESULTS_DIR}/benchmark_envoy.json" \
        --raw-output "${RESULTS_DIR}/benchmark_envoy_raw.log" || {
        log "ERROR: Envoy single-machine benchmark failed"
        return 50
    }
}

stop_envoy_process() {
    local pid_file="$1" pid
    [[ -f "${pid_file}" ]] || return 0
    pid="$(<"${pid_file}")"
    if [[ "${pid}" =~ ^[0-9]+$ ]] && kill -0 "${pid}" 2>/dev/null; then
        kill "${pid}" 2>/dev/null || true
        for _ in {1..10}; do
            kill -0 "${pid}" 2>/dev/null || break
            sleep 1
        done
        kill -0 "${pid}" 2>/dev/null && kill -9 "${pid}" 2>/dev/null || true
    fi
    rm -f "${pid_file}"
}

stop_envoy_runtime() {
    initialize_runtime || return $?
    stop_envoy_process "${DIRECT_PID_FILE}"
    stop_envoy_process "${PROXY_PID_FILE}"
    stop_envoy_process "${BACKEND_PID_FILE}"
    rm -rf -- "${RUNTIME_DIR}"
    log "stopped isolated Envoy benchmark services"
}

standalone_runtime() {
    python3 "${SCRIPT_DIR}/scripts/standalone_runtime.py" "$@"
}

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping standalone work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    if [[ "${STANDALONE_OWNS_WORK_DIR}" -ne 1 ]]; then
        log "external work directory was not removed: ${PERF_WORK_DIR}"
        return 0
    fi
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-perf/envoy/local-* && "${PERF_WORK_DIR}" != '/home/runner/boostkit-perf/envoy' ]] || {
        log "ERROR: refusing to clean unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    rm -rf -- "${PERF_WORK_DIR}"
}

run_envoy_standalone() {
    local stage_status=0 failed_stage='' cleanup_status='passed' command_status='passed'
    initialize_runtime || return $?
    : > "${RESULTS_DIR}/results.log"
    standalone_runtime system "${RESULTS_DIR}/system_info.json"
    standalone_runtime runtime "${RESULTS_DIR}/runtime_before.json"
    if build_envoy; then
        :
    else
        stage_status=$?
        failed_stage='build'
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if start_envoy_runtime; then
            :
        else
            stage_status=$?
            failed_stage='start'
        fi
    fi
    if [[ "${stage_status}" -eq 0 ]]; then
        if run_envoy_benchmarks; then
            :
        else
            stage_status=$?
            failed_stage='test'
        fi
    fi
    stop_envoy_runtime || { cleanup_status='failed'; stage_status=70; }
    standalone_runtime runtime "${RESULTS_DIR}/runtime_after.json"
    standalone_runtime build-info "${RESULTS_DIR}/build_info.json" \
        "${SOFTWARE_VERSION}" "${PERF_ACTUAL_VERSION_FILE}" "${EXPECTED_ARCH}" "${PERF_RUN_ID}"
    [[ "${stage_status}" -eq 0 ]] || command_status='failed'
    standalone_runtime finalize "${RESULTS_DIR}" "${SOFTWARE_VERSION}" "${EXPECTED_ARCH}" \
        "${PERF_RUN_ID}" "${command_status}" "${cleanup_status}" "${failed_stage}"
    return "${stage_status}"
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]

Build and benchmark official Envoy ${SOFTWARE_VERSION} in two isolated local
HTTPS scenarios: direct response and reverse proxy. Results default to
results/<version>/<run-id>/ inside this directory.

Options:
  --version VERSION       Envoy version (default: ${SOFTWARE_VERSION})
  --results-dir DIR       Persistent result directory
  --keep-workdir          Keep the isolated work directory for debugging
  -h, --help              Show this help
USAGE
}

main() {
    while [[ "$#" -gt 0 ]]; do
        case "$1" in
            --version) SOFTWARE_VERSION="$2"; shift 2 ;;
            --results-dir) RESULTS_DIR="$2"; shift 2 ;;
            --keep-workdir) STANDALONE_KEEP_WORK_DIR=1; shift ;;
            -h|--help) usage; return 0 ;;
            *) log "ERROR: unknown option: $1"; usage >&2; return 10 ;;
        esac
    done
    trap 'stop_envoy_runtime >/dev/null 2>&1 || true' EXIT
    run_envoy_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
