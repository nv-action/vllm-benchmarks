#!/usr/bin/env bash
# test-case-007: compare apt/yum + pip install time under two network environments.
#
#   scenario=squid-proxy    : keep the default squid http_proxy, pip uses tuna mirror
#                             (the "default setting" of the buildkit runner).
#   scenario=cache-service  : unset http_proxy, route apt/yum + pip through the
#                             cluster-local cache-service (APTMIRROR/PIP_INDEX_URL/...).
#
# The install steps are extracted from Dockerfile.a3 (apt) / Dockerfile.a3.openEuler (yum),
# restricted to the parts that do NOT need the CANN toolkit or a vllm source checkout:
#   - apt-get update / yum update
#   - apt-get install / yum install  (git vim wget ... clang)
#   - pip install mooncake-transfer-engine-npu
#   - pip install modelscope 'ray>=...' 'protobuf>...'
#
# Usage:
#   bash install-env.sh --os apt|yum --scenario squid-proxy|cache-service
#
# Outputs (override dir with RESULTS_DIR):
#   $RESULTS_DIR/timings-<os>-<scenario>.tsv  phase<TAB>seconds<TAB>scenario<TAB>os
#   $RESULTS_DIR/log-<os>-<scenario>.txt      full install log

set -uo pipefail

OS=""
SCENARIO=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --os) OS="$2"; shift 2 ;;
        --scenario) SCENARIO="$2"; shift 2 ;;
        *) echo "[error] unknown arg: $1"; exit 2 ;;
    esac
done

case "$OS" in
    apt | yum) ;;
    *) echo "[error] --os must be apt or yum (got '${OS}')"; exit 2 ;;
esac
case "$SCENARIO" in
    squid-proxy | cache-service) ;;
    *) echo "[error] --scenario must be squid-proxy or cache-service (got '${SCENARIO}')"; exit 2 ;;
esac

# ---- defaults ---------------------------------------------------------------
SQUID_PROXY="${SQUID_PROXY:-http://squid-cache.squid.svc.cluster.local:3128}"
CACHE_SERVICE="${CACHE_SERVICE:-http://cache-service.nginx-pypi-cache.svc.cluster.local:8081}"
TUNA_INDEX="https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple"

RESULTS_DIR="${RESULTS_DIR:-/tmp/test-case-007-results}"
LOG="$RESULTS_DIR/log-${OS}-${SCENARIO}.txt"
TSV="$RESULTS_DIR/timings-${OS}-${SCENARIO}.tsv"
mkdir -p "$RESULTS_DIR"
: > "$LOG"
printf 'phase\tseconds\tscenario\tos\n' > "$TSV"

log() { echo "[$(date '+%H:%M:%S')] $*" | tee -a "$LOG"; }

# ---- helpers ----------------------------------------------------------------
# Run a phase with timing. Never aborts the script: failures are recorded as data.
run_timed() {
    local phase="$1"
    shift
    local start end status sec
    start=$(date +%s)
    "$@" 2>&1 | tee -a "$LOG"
    status=${PIPESTATUS[0]}
    end=$(date +%s)
    sec=$((end - start))
    printf '%s\t%s\t%s\t%s\n' "$phase" "$sec" "$SCENARIO" "$OS" >> "$TSV"
    if [[ $status -eq 0 ]]; then
        log "### [${phase}] OK   ${sec}s"
    else
        log "### [${phase}] FAIL (exit=${status})  ${sec}s"
    fi
    return 0
}

# If squid MITM CA is mounted, append it to certifi so pip trusts the proxy.
trust_squid_ca_for_pip() {
    local ca_file="/etc/squid-ca/squid-ca.pem"
    [[ -f "$ca_file" ]] || { log "[setup] WARNING: squid CA not found at $ca_file"; return 0; }
    if ! python3 -c 'import certifi' >/dev/null 2>&1; then
        log "[setup] certifi not present yet; will re-trust after pip install"
        return 0
    fi
    local certifi
    certifi="$(python3 -c 'import certifi; print(certifi.where())' 2>/dev/null)" || return 0
    if [[ -n "$certifi" && -f "$certifi" ]]; then
        cp "$certifi" "${certifi}.orig.$$"
        cat "$ca_file" >> "$certifi"
        export REQUESTS_CA_BUNDLE="$certifi"
        export SSL_CERT_FILE="$certifi"
        log "[setup] squid CA appended to certifi: $certifi"
    fi
}

setup_network() {
    case "$SCENARIO" in
        squid-proxy)
            export HTTP_PROXY="${SQUID_PROXY}" HTTPS_PROXY="${SQUID_PROXY}"
            export http_proxy="${SQUID_PROXY}" https_proxy="${SQUID_PROXY}"
            export PIP_INDEX_URL="${PIP_INDEX_URL:-$TUNA_INDEX}"
            log "[net] squid-proxy: HTTP(S)_PROXY=$SQUID_PROXY PIP_INDEX_URL=$PIP_INDEX_URL"
            trust_squid_ca_for_pip
            ;;
        cache-service)
            unset HTTP_PROXY HTTPS_PROXY http_proxy https_proxy all_proxy ALL_PROXY
            export APTMIRROR="${APTMIRROR:-$CACHE_SERVICE}"
            export PIP_INDEX_URL="${PIP_INDEX_URL:-${CACHE_SERVICE}/pypi/simple}"
            export PIP_TRUSTED_HOST="${PIP_TRUSTED_HOST:-cache-service.nginx-pypi-cache.svc.cluster.local}"
            export PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-${CACHE_SERVICE}/whl/cpu}"
            export ASCEND_INDEX_URL="${ASCEND_INDEX_URL:-${CACHE_SERVICE}/ascend/repos/pypi}"
            export GIT_PROXY="${GIT_PROXY:-https://gh-proxy.test.osinfra.cn/}"
            log "[net] cache-service: proxy unset, APTMIRROR=$APTMIRROR PIP_INDEX_URL=$PIP_INDEX_URL"
            ;;
    esac
    export NO_PROXY="localhost,127.0.0.1,.svc.cluster.local,.cluster.local"
    export no_proxy="$NO_PROXY"
}

# apt sources -> cache-service (assumes mirror layout ${CACHE_SERVICE}/ubuntu; override APT_MIRROR_URL).
rewrite_apt_sources() {
    local mirror="${APT_MIRROR_URL:-${CACHE_SERVICE}/ubuntu}"
    local codename
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
    log "[apt] rewriting sources -> ${mirror} (codename=${codename})"
    find /etc/apt -maxdepth 1 -name 'sources.list*' -exec mv {} {}.bak.$$ \; 2>/dev/null || true
    find /etc/apt/sources.list.d -maxdepth 1 -type f -exec mv {} {}.bak.$$ \; 2>/dev/null || true
    cat > /etc/apt/sources.list.d/cache-service.sources <<EOF
Types: deb
URIs: ${mirror}
Suites: ${codename} ${codename}-updates ${codename}-security
Components: main universe multiverse
Signed-By: /usr/share/keyrings/ubuntu-archive-keyring.gpg
EOF
}

# yum repos -> cache-service (assumes mirror layout ${CACHE_SERVICE}/openeuler; override YUM_MIRROR_URL).
rewrite_yum_sources() {
    local mirror="${YUM_MIRROR_URL:-${CACHE_SERVICE}/openeuler}"
    log "[yum] rewriting repos -> ${mirror}"
    find /etc/yum.repos.d -maxdepth 1 -name '*.repo' -exec mv {} {}.bak.$$ \; 2>/dev/null || true
    cat > /etc/yum.repos.d/cache-service.repo <<EOF
[cache-service]
name=cache-service
baseurl=${mirror}/\$releasever/os/\$basearch/
enabled=1
gpgcheck=0
repo_gpgcheck=0
EOF
}

# ---- extracted install steps -------------------------------------------------
# Dockerfile.a3 (apt)
APT_PKGS="git vim wget net-tools gcc g++ cmake numactl libnuma-dev libibverbs-dev libjemalloc2 libhiredis-dev clang-15"
# Dockerfile.a3.openEuler (yum)
YUM_PKGS="git vim wget net-tools gcc gcc-c++ make cmake numactl numactl-devel libibverbs-devel jemalloc hiredis-devel clang patch"
MOONCAKE_TAG="0.3.11.post1"

log "=== start OS=${OS} SCENARIO=${SCENARIO} ==="
setup_network
if [[ "$SCENARIO" == "cache-service" ]]; then
    if [[ "$OS" == "apt" ]]; then
        rewrite_apt_sources
    else
        rewrite_yum_sources
    fi
fi

# The two Dockerfiles assume pip is preinstalled (cann base image). The test base
# images may not have it, so add python3-pip into the (timed) package install if missing.
EXTRA=""
if ! python3 -m pip --version >/dev/null 2>&1; then
    EXTRA="python3-pip"
    log "[setup] pip missing, will add '${EXTRA}' to the package install"
fi

if [[ "$OS" == "apt" ]]; then
    PKGS="$APT_PKGS $EXTRA"
    run_timed apt_update  apt-get update -y
    run_timed apt_install apt-get install -y $PKGS
    update-alternatives --install /usr/bin/clang clang /usr/bin/clang-15 20 >/dev/null 2>&1 || true
    update-alternatives --install /usr/bin/clang++ clang++ /usr/bin/clang++-15 20 >/dev/null 2>&1 || true
else
    PKGS="$YUM_PKGS $EXTRA"
    run_timed yum_update  yum update -y
    run_timed yum_install yum install -y $PKGS
fi

# Re-trust squid CA now that certifi exists (scenario squid-proxy only).
if [[ "$SCENARIO" == "squid-proxy" ]]; then
    trust_squid_ca_for_pip
fi

# pip index = scenario value (matches `pip config set global.index-url ${PIP_INDEX_URL}`)
run_timed pip_config bash -c "python3 -m pip config set global.index-url '${PIP_INDEX_URL}'"

# pip installs extracted from the Dockerfiles (mooncake + modelscope/ray/protobuf)
run_timed pip_mooncake bash -c "python3 -m pip install mooncake-transfer-engine-npu==${MOONCAKE_TAG} --extra-index-url https://mirrors.aliyun.com/pypi/web/simple"
run_timed pip_misc     bash -c "python3 -m pip install modelscope 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0'"

echo ""
echo "################ SUMMARY (${OS} / ${SCENARIO}) ################"
if command -v column >/dev/null 2>&1; then
    column -t -s $'\t' < "$TSV"
else
    cat "$TSV"
fi
echo "results dir: $RESULTS_DIR"
