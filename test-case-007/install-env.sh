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
    local py="${PYTHON_CMD:-python3}"
    [[ -f "$ca_file" ]] || { log "[setup] WARNING: squid CA not found at $ca_file"; return 0; }
    if ! "$py" -c 'import certifi' >/dev/null 2>&1; then
        log "[setup] certifi not present yet; will re-trust after pip install"
        return 0
    fi
    local certifi
    certifi="$("$py" -c 'import certifi; print(certifi.where())' 2>/dev/null)" || return 0
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
            # The runner pod sets these to the squid MITM CA; they would break
            # direct TLS (e.g. pip following the /pypi/simple 301 to
            # mirrors.huaweicloud.com), so drop them and use the default trust store.
            unset PIP_CERT SSL_CERT_FILE REQUESTS_CA_BUNDLE CURL_CA_BUNDLE GIT_SSL_CAINFO
            export APTMIRROR="${APTMIRROR:-$CACHE_SERVICE}"
            # The cache-service pypi endpoint (/pypi/simple) 301-redirects to
            # mirrors.huaweicloud.com, which answers pip's PEP 691 Accept header
            # with a generic portal page (0 package links), so pip can never
            # resolve anything. Fall back to the tuna mirror for the pip steps.
            export PIP_INDEX_URL="${PIP_INDEX_URL:-$TUNA_INDEX}"
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
    local codename keyring signed_by
    codename="$(. /etc/os-release && echo "${VERSION_CODENAME:-noble}")"
    log "[apt] rewriting sources -> ${mirror} (codename=${codename})"
    find /etc/apt -maxdepth 1 -name 'sources.list*' -exec mv {} {}.bak.$$ \; 2>/dev/null || true
    # Some base images (e.g. CANN) have no /etc/apt/sources.list.d/ at all;
    # create it before writing the deb822 source file.
    mkdir -p /etc/apt/sources.list.d
    find /etc/apt/sources.list.d -maxdepth 1 -type f -exec mv {} {}.bak.$$ \; 2>/dev/null || true
    keyring=/usr/share/keyrings/ubuntu-archive-keyring.gpg
    if [[ -f "$keyring" ]]; then
        signed_by="Signed-By: $keyring"
    else
        signed_by="Trusted: yes"
    fi
    cat > /etc/apt/sources.list.d/cache-service.sources <<EOF
Types: deb
URIs: ${mirror}
Suites: ${codename} ${codename}-updates ${codename}-security
Components: main universe multiverse
${signed_by}
EOF
}

# yum repos -> cache-service. Keep the image's default repo set (OS,
# everything, EPOL, update, ...) but point baseurl at the cache-service
# mirror, which mirrors openEuler under /openeuler/<repo-dir>/ (e.g.
# openEuler-24.03-LTS-SP3/OS/$basearch/). Override the mirror base with
# YUM_MIRROR_URL (replaces https://repo.openeuler.org).
rewrite_yum_sources() {
    local mirror="${YUM_MIRROR_URL:-${CACHE_SERVICE}/openeuler}"
    log "[yum] rewriting repos -> ${mirror}/<repo-dir>/"
    mkdir -p /etc/yum.repos.d
    local f
    for f in /etc/yum.repos.d/*.repo; do
        [[ -f "$f" ]] || continue
        cp "$f" "$f.bak.$$"
    done
    sed -i "s|https://repo.openeuler.org|${mirror}|g" /etc/yum.repos.d/*.repo
    # plain-http mirror: drop gpg checks and skip any subrepo missing on it
    sed -i -e 's/^gpgcheck=.*/gpgcheck=0/' \
        -e 's/^repo_gpgcheck=.*/repo_gpgcheck=0/' \
        -e '/^\[/a skip_if_unavailable=True' \
        /etc/yum.repos.d/*.repo
}

# The runner pod template overrides PATH and drops the CANN image's python
# dir (e.g. /usr/local/python3.12.13/bin), so `python3` may be missing or
# resolve to the system python (3.10, pip 22.0.2) which mishandles modern
# PEP 691 JSON simple indexes. Prefer the image's own python/pip (modern),
# fall back to whatever `python3` resolves to.
find_python() {
    local py
    for py in python3.12 /usr/local/python3.12.13/bin/python3 /usr/local/bin/python3 python3 /usr/bin/python3; do
        if command -v "$py" >/dev/null 2>&1 && "$py" -m pip --version >/dev/null 2>&1; then
            echo "$py"
            return 0
        fi
    done
    return 1
}

# ---- extracted install steps -------------------------------------------------
# Dockerfile.a3 (apt)
APT_PKGS="git vim wget net-tools gcc g++ cmake numactl libnuma-dev libibverbs-dev libjemalloc2 libhiredis-dev clang-15"
# Dockerfile.a3.openEuler (yum)
YUM_PKGS="git vim wget net-tools gcc gcc-c++ make cmake numactl numactl-devel libibverbs-devel jemalloc hiredis-devel clang patch"
MOONCAKE_TAG="0.3.11.post1"

log "=== start OS=${OS} SCENARIO=${SCENARIO} ==="

# Prefer the image's own python/pip (e.g. CANN's python3.12+pip 26) so the
# installs behave like the real Dockerfile build; only fall back to
# installing python3-pip if none is found.
EXTRA=""
PYTHON_CMD="$(find_python || true)"
if [[ -z "$PYTHON_CMD" ]]; then
    EXTRA="python3-pip"
    log "[setup] pip missing, will add '${EXTRA}' to the package install"
else
    log "[setup] using python: $PYTHON_CMD ($($PYTHON_CMD -m pip --version))"
fi

setup_network
if [[ "$SCENARIO" == "cache-service" ]]; then
    if [[ "$OS" == "apt" ]]; then
        rewrite_apt_sources
    else
        rewrite_yum_sources
    fi
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

# python3-pip may have been installed just now; re-resolve if we had none.
if [[ -z "$PYTHON_CMD" ]]; then
    PYTHON_CMD="$(find_python || true)"
    log "[setup] after pkg install, using python: ${PYTHON_CMD:-<none>}"
fi

# Re-trust squid CA now that certifi exists (scenario squid-proxy only).
if [[ "$SCENARIO" == "squid-proxy" ]]; then
    trust_squid_ca_for_pip
fi

# pip index = scenario value (matches `pip config set global.index-url ${PIP_INDEX_URL}`)
run_timed pip_config bash -c "$PYTHON_CMD -m pip config set global.index-url '${PIP_INDEX_URL}'"

# pip installs extracted from the Dockerfiles (mooncake + modelscope/ray/protobuf)
run_timed pip_mooncake bash -c "$PYTHON_CMD -m pip install mooncake-transfer-engine-npu==${MOONCAKE_TAG} --extra-index-url https://mirrors.aliyun.com/pypi/web/simple"
run_timed pip_misc     bash -c "$PYTHON_CMD -m pip install modelscope 'ray>=2.47.1,<=2.48.0' 'protobuf>3.20.0'"

echo ""
echo "################ SUMMARY (${OS} / ${SCENARIO}) ################"
if command -v column >/dev/null 2>&1; then
    column -t -s $'\t' < "$TSV"
else
    cat "$TSV"
fi
echo "results dir: $RESULTS_DIR"
