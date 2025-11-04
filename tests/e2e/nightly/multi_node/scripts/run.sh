#!/bin/bash
set -euo pipefail

# Color definitions
GREEN="\033[0;32m"
BLUE="\033[0;34m"
YELLOW="\033[0;33m"
RED="\033[0;31m"
NC="\033[0m" # No Color

# Configuration
LOG_DIR="/root/.cache/tests/logs"
OVERWRITE_LOGS=true
export LD_LIBRARY_PATH=/usr/local/Ascend/ascend-toolkit/latest/python/site-packages:$LD_LIBRARY_PATH

# Function to print section headers
print_section() {
    echo -e "\n${BLUE}=== $1 ===${NC}"
}

print_failure() {
    echo -e "${RED}${FAIL_TAG} ✗ ERROR: $1${NC}"
    exit 1
}

# Function to print success messages
print_success() {
    echo -e "${GREEN}✓ $1${NC}"
}

# Function to print error messages and exit
print_error() {
    echo -e "${RED}✗ ERROR: $1${NC}"
    exit 1
}

# Function to check command success
check_success() {
    if [ $? -ne 0 ]; then
        print_error "$1"
    fi
}

if [ $(id -u) -ne 0 ]; then
	print_error "Require root permission, try sudo ./dependencies.sh"
fi


check_npu_info() {
    echo "====> Check NPU info"
    npu-smi info
    cat "/usr/local/Ascend/ascend-toolkit/latest/$(uname -i)-linux/ascend_toolkit_install.info"
}

check_and_config() {
    echo "====> Configure mirrors and git proxy"
    git config --global url."https://gh-proxy.test.osinfra.cn/https://github.com/".insteadOf "https://github.com/"
    pip config set global.index-url https://mirrors.tuna.tsinghua.edu.cn/pypi/web/simple
    export PIP_EXTRA_INDEX_URL=https://mirrors.huaweicloud.com/ascend/repos/pypi
}

checkout_src() {
    echo "====> Checkout source code"

    # vllm-ascend
    if [ ! -d "$WORKSPACE/vllm-ascend" ]; then
        git clone --depth 1 -b $VLLM_ASCEND_VERSION $VLLM_ASCEND_REMOTE_URL "$WORKSPACE/vllm-ascend"
    fi
}

install_sys_dependencies() {
    echo "====> Install system dependencies"
    apt-get update -y

    DEP_LIST=()
    while IFS= read -r line; do
        [[ -n "$line" && ! "$line" =~ ^# ]] && DEP_LIST+=("$line")
    done < "$WORKSPACE/vllm-ascend/packages.txt"

    apt-get install -y "${DEP_LIST[@]}" gcc g++ cmake libnuma-dev iproute2
}

install_vllm() {
    echo "====> Install vllm-ascend"
    export PIP_EXTRA_INDEX_URL=https://mirrors.huaweicloud.com/ascend/repos/pypi
    pip install -e "$WORKSPACE/vllm-ascend"
    # Install for pytest
    pip install -r "$WORKSPACE/vllm-ascend/requirements-dev.txt"
}

install_ais_bench() {
    local AIS_BENCH="$WORKSPACE/vllm-ascend/benchmark"
    git clone -b v3.0-20250930-master --depth 1 https://gitee.com/aisbench/benchmark.git $AIS_BENCH
    cd $AIS_BENCH
    git checkout v3.0-20250930-master
    pip3 install -e ./
    pip3 install -r requirements/api.txt
    pip3 install -r requirements/extra.txt
    cd -
}

kill_npu_processes() {
  pgrep python3 | xargs -r kill -9
  pgrep VLLM | xargs -r kill -9

  sleep 4
}

run_tests_with_log() {
    set +e
    kill_npu_processes
    BASENAME=$(basename "$CONFIG_YAML_PATH" .yaml)
    # each worker should have log file
    LOG_FILE="${RESULT_FILE_PATH}/${BASENAME}_worker_${LWS_WORKER_INDEX}.log"
    mkdir -p ${RESULT_FILE_PATH}
    pytest -sv tests/e2e/nightly/multi_node/test_multi_node.py 2>&1 | tee $LOG_FILE
    ret=${PIPESTATUS[0]}
    set -e
    if [ "$LWS_WORKER_INDEX" -eq 0 ]; then
        if [ $ret -eq 0 ]; then
            print_success "All tests passed!"
        else
            print_failure "Some tests failed!"
            mv LOG_FILE error_${LOG_FILE}
        fi
    fi
}

main() {
    check_npu_info
    check_and_config
    checkout_src
    install_sys_dependencies
    install_vllm
    install_ais_bench
    # to speed up mooncake build process, install Go here
    cd "$WORKSPACE/vllm-ascend"
    run_tests_with_log
}

main "$@"
