#!/usr/bin/env bash
# =============================================================================
# test-squid run-suite.sh —— CI 下载/上传链路测试主脚本
#
# 说明：
#   - Squid 代理已由 runner / 工作负载 pod 注入（HTTP(S)_PROXY + MITM CA），
#     本脚本【不配置任何代理】，只复用 pod 注入的环境。
#   - 命令提取自 vllm-ascend CI 真实下载/上传代码，只把「载荷」缩小（mock），
#     保证每条链路的网络行为不变但耗时可控。
#   - 每个阶段独立计时、失败不影响其它阶段（阶段级 continue-on-error）。
#
# 用法：
#   bash test-squid/run-suite.sh
#
# 环境变量（均可覆盖）：
#   RESULTS_DIR       结果目录，默认 /tmp/test-squid-results
#   PIP_INDEX_URL     pip 源，默认 https://repo.huaweicloud.com/repository/pypi/simple
#   PYTORCH_INDEX_URL pytorch CPU wheel 源，默认 https://download.pytorch.org/whl/cpu/
#   ASCEND_INDEX_URL  ascend 私源，默认 https://repo.huaweicloud.com/ascend/repos/pypi
#   APTMIRROR         apt 镜像（仅打印参考，本脚本不改源），默认 https://repo.huaweicloud.com
#   TEST_SQUID_PIP_MODELSCOPE 1=额外 pip install modelscope（走真实大包，默认 0 跳过）
#   TEST_SQUID_MODEL_DOWNLOAD 1=额外 modelscope 下载极小模型（默认 0 跳过）
#   TEST_SQUID_GIT_CLONE      1=额外浅克隆小仓库（默认 1）
#   TEST_SQUID_OBS_WGET       1=额外 OBS 路径小文件 wget（默认 1）
#   TEST_SQUID_SKOPEO         1=额外 skopeo copy（需 SWR_USERNAME/SWR_PASSWORD，默认 0）
#   TEST_SQUID_GIT_PUSH       1=额外 git push 探测分支（需 GH_TOKEN，默认 0）
#
# 输出：
#   $RESULTS_DIR/timings.tsv      phase<TAB>seconds<TAB>status<TAB>category
#   $RESULTS_DIR/env.txt          注入环境快照
#   $RESULTS_DIR/probes.tsv       直连探测结果
#   $RESULTS_DIR/payload.bin      上传链路用的小载荷文件
# =============================================================================
set -uo pipefail

RESULTS_DIR="${RESULTS_DIR:-/tmp/test-squid-results}"
PIP_INDEX_URL="${PIP_INDEX_URL:-https://repo.huaweicloud.com/repository/pypi/simple}"
PYTORCH_INDEX_URL="${PYTORCH_INDEX_URL:-https://download.pytorch.org/whl/cpu/}"
ASCEND_INDEX_URL="${ASCEND_INDEX_URL:-https://repo.huaweicloud.com/ascend/repos/pypi}"
APTMIRROR="${APTMIRROR:-https://repo.huaweicloud.com}"
TEST_SQUID_PIP_MODELSCOPE="${TEST_SQUID_PIP_MODELSCOPE:-0}"
TEST_SQUID_MODEL_DOWNLOAD="${TEST_SQUID_MODEL_DOWNLOAD:-0}"
TEST_SQUID_GIT_CLONE="${TEST_SQUID_GIT_CLONE:-1}"
TEST_SQUID_OBS_WGET="${TEST_SQUID_OBS_WGET:-1}"
TEST_SQUID_SKOPEO="${TEST_SQUID_SKOPEO:-0}"
TEST_SQUID_GIT_PUSH="${TEST_SQUID_GIT_PUSH:-0}"

mkdir -p "$RESULTS_DIR"
TSV="$RESULTS_DIR/timings.tsv"
PROBES="$RESULTS_DIR/probes.tsv"
printf 'phase\tseconds\tstatus\tcategory\n' > "$TSV"
printf 'url\tstatus\tseconds\n' > "$PROBES"

log() { echo "[$(date '+%H:%M:%S')] $*"; }

# 阶段计时封装：任何失败都记录为数据，不中断后续阶段
run_timed() {
    local phase="$1" category="$2"
    shift 2
    local start end status sec
    start=$(date +%s)
    "$@" >/tmp/test-squid-phase.log 2>&1
    status=$?
    end=$(date +%s)
    sec=$((end - start))
    printf '%s\t%s\t%s\t%s\n' "$phase" "$sec" "$status" "$category" >> "$TSV"
    if [[ $status -eq 0 ]]; then
        log "### [${phase}] OK   ${sec}s"
    else
        log "### [${phase}] FAIL(exit=${status})  ${sec}s  (tail 见下)"
        tail -n 5 /tmp/test-squid-phase.log | sed 's/^/    | /'
    fi
    return 0
}

# 判定包管理器：apt / yum / dnf
detect_pkgmgr() {
    if command -v apt-get >/dev/null 2>&1; then echo apt
    elif command -v dnf >/dev/null 2>&1; then echo dnf
    elif command -v yum >/dev/null 2>&1; then echo yum
    else echo none; fi
}

# ---------------------------------------------------------------------------
# 0. 环境快照：确认 runner pod 注入的 squid 代理环境（只读，不做任何配置）
# ---------------------------------------------------------------------------
log "== 环境探测 =="
{
    echo "---- proxy 相关 env ----"
    env | grep -iE 'proxy|ca_|_ca|pip_cert|ssl_cert' | sort || true
    echo
    echo "---- 包管理器 ----"
    echo "pkgmgr=$(detect_pkgmgr)"
    echo
    echo "---- squid CA 是否存在 ----"
    ls -l /etc/squid-ca/squid-ca.pem 2>/dev/null || echo "(no /etc/squid-ca/squid-ca.pem)"
} | tee "$RESULTS_DIR/env.txt"

# ---------------------------------------------------------------------------
# 1. 系统包管理器下载（提取自 _schedule_image_build.yaml / _build_csrc_cache.yaml）
#    mock：只装 zstd 小包，不装 packages.txt 全集
# ---------------------------------------------------------------------------
PKGMGR=$(detect_pkgmgr)
case "$PKGMGR" in
    apt)
        log "== apt 下载链路 =="
        run_timed apt-update download apt-get update -y
        run_timed apt-install-zstd download apt-get install -y zstd
        ;;
    dnf|yum)
        log "== $PKGMGR 下载链路 =="
        run_timed "$PKGMGR-update" download "$PKGMGR" install -y zstd
        ;;
    *)
        log "[skip] 未识别包管理器"
        ;;
esac

# ---------------------------------------------------------------------------
# 2. Python 包下载（提取自 _schedule_image_build.yaml build-args /
#    _build_csrc_cache.yaml UV_* / install_daily_deps.sh）
#    与 CI 相同的 index 配置
# ---------------------------------------------------------------------------
log "== pip/uv 下载链路 =="
export PIP_INDEX_URL
export PYTORCH_INDEX_URL
export ASCEND_INDEX_URL
python3 -m pip config set global.index-url "$PIP_INDEX_URL" >/dev/null 2>&1 || true

# 2.1 pip 安装小轮子（mock 掉 requirements-dev.txt 全量）
run_timed pip-install-small download python3 -m pip install --no-cache-dir zstandard

# 2.2 uv：仅当存在时验证 extra index 解析（mock：只下载小包）
if command -v uv >/dev/null 2>&1; then
    run_timed uv-download-small download \
        uv pip download --no-deps \
        --index-url "$PIP_INDEX_URL" \
        --extra-index-url "$ASCEND_INDEX_URL" \
        --no-cache zstandard -d /tmp/test-squid-uv
else
    log "### [uv-download-small] 容器内无 uv，跳过（不视为失败）"
fi

# 2.3 pytorch CPU wheel 索引探测（mock：只取元数据，不拉 ~100MB wheel）
run_timed pytorch-index-probe download \
    python3 -m pip index versions torch --index-url "$PYTORCH_INDEX_URL"

# 2.4 可选：modelscope 安装（真实大包，验证 pip 大包链路）
if [[ "$TEST_SQUID_PIP_MODELSCOPE" == "1" ]]; then
    run_timed pip-install-modelscope download \
        python3 -m pip install --no-cache-dir modelscope
fi

# ---------------------------------------------------------------------------
# 3. 模型/数据集下载（提取自 labeled_download_model_dataset.yaml）
#    mock：默认只验证 modelscope 可用；TEST_SQUID_MODEL_DOWNLOAD=1 时下极小模型
# ---------------------------------------------------------------------------
if [[ "$TEST_SQUID_MODEL_DOWNLOAD" == "1" ]]; then
    log "== modelscope 下载链路 =="
    run_timed modelscope-download-tiny download \
        python3 -m modelscope download --model "AI-ModelScope/bert-base-uncased" --local_dir /tmp/test-squid-model
fi

# ---------------------------------------------------------------------------
# 4. Git / GitHub 内容下载（提取自 Dockerfile.nightly.a3 的 git clone、
#    _build_csrc_cache.yaml 的 git fetch；actions/checkout 在 workflow 层）
#    mock：ls-remote + 浅克隆极小仓库
# ---------------------------------------------------------------------------
log "== git 下载链路 =="
run_timed git-ls-remote-github download \
    git ls-remote https://github.com/AISBench/benchmark.git HEAD

if [[ "$TEST_SQUID_GIT_CLONE" == "1" ]]; then
    run_timed git-shallow-clone-tiny download \
        git clone --depth 1 --filter=blob:none \
        https://github.com/octocat/Hello-World.git /tmp/test-squid-hello
fi

# ---------------------------------------------------------------------------
# 5. OBS 对象下载路径（提取自 install_daily_deps.sh 的 wget OBS wheel）
#    mock：wget 同域小对象（PyPI 镜像首页），验证 OBS/华为云对象下载路径
# ---------------------------------------------------------------------------
if [[ "$TEST_SQUID_OBS_WGET" == "1" ]]; then
    log "== OBS/对象下载探测 =="
    run_timed wget-small-object download \
        wget -q -O /tmp/test-squid-obstest \
        "https://repo.huaweicloud.com/repository/pypi/simple/zstandard/"
    run_timed obs-head-probe probe \
        curl -sS -I --max-time 30 "https://pytorch-package.obs.cn-north-4.myhuaweicloud.com/"
fi

# ---------------------------------------------------------------------------
# 6. 直连探测：各关键域名连通性 + 延迟（同 check_md_links / 各 workflow 网络面）
# ---------------------------------------------------------------------------
log "== 直连探测 =="
probe() {
    local url="$1"
    local start end sec
    start=$(date +%s)
    code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 30 -I "$url" 2>/dev/null)
    status=$?
    end=$(date +%s)
    sec=$((end - start))
    if [[ $status -eq 0 ]]; then
        printf '%s\t%s\t%s\n' "$url" "$code" "$sec" >> "$PROBES"
        log "  probe OK  $code  ${sec}s  $url"
    else
        printf '%s\tERR(%s)\t%s\n' "$url" "$status" "$sec" >> "$PROBES"
        log "  probe FAIL($status)  ${sec}s  $url"
    fi
}
for url in \
    "https://github.com" \
    "https://raw.githubusercontent.com" \
    "$PIP_INDEX_URL" \
    "$PYTORCH_INDEX_URL" \
    "$ASCEND_INDEX_URL" \
    "https://modelscope.cn" \
    "https://swr.cn-southwest-2.myhuaweicloud.com" \
    "https://obs.cn-north-4.myhuaweicloud.com" \
    "https://gh-proxy.test.osinfra.cn" \
    "https://repo.huaweicloud.com" \
    "https://mirrors.aliyun.com" \
    "https://mirrors.tuna.tsinghua.edu.cn" \
    "https://download.pytorch.org" ; do
    probe "$url"
done

# ---------------------------------------------------------------------------
# 7. 上传链路：生成小载荷（随后由 workflow 完成 artifact / cache 的
#    真实上传；skopeo / git push 在本脚本内可选执行，凭据不足时跳过）
# ---------------------------------------------------------------------------
log "== 上传载荷准备 =="
head -c 1048576 /dev/urandom > "$RESULTS_DIR/payload.bin"
sha256sum "$RESULTS_DIR/payload.bin" | tee "$RESULTS_DIR/payload.sha256"
log "payload.bin 已生成: $(ls -l "$RESULTS_DIR/payload.bin" | awk '{print $5}') bytes"

# 7.1 skopeo copy（提取自 _schedule_image_build.yaml 的 "Tag digest to prevent GC"）
#     上传方向：SWR 临时仓打 tag。需 SWR_USERNAME/SWR_PASSWORD，失败仅记录。
SWR_TEMP_REPO="${SWR_TEMP_REPO:-swr.cn-southwest-2.myhuaweicloud.com/atlas_ci_cq/vllm-atlas-temp}"
if [[ "$TEST_SQUID_SKOPEO" == "1" && -n "${SWR_USERNAME:-}" && -n "${SWR_PASSWORD:-}" ]]; then
    log "== skopeo copy 上传链路 =="
    command -v skopeo >/dev/null 2>&1 || apt-get install -y -qq skopeo >/dev/null 2>&1 || true
    TS=$(date +%Y%m%d%H%M%S)
    CREDS="$SWR_USERNAME:$SWR_PASSWORD"
    # mock：对临时仓一个已存在的基础 tag 复制出测试 tag；源 tag 不存在则记录失败
    run_timed skopeo-copy-tag upload \
        skopeo copy --src-creds "$CREDS" --dest-creds "$CREDS" \
        "docker://${SWR_TEMP_REPO}:main" \
        "docker://${SWR_TEMP_REPO}:test-squid-${TS}"
else
    log "[skip] TEST_SQUID_SKOPEO != 1 或无 SWR 凭据，跳过 skopeo copy"
fi

# 7.2 git push 探测（提取自 main2main-push-probe.yaml 的 _push_via_proxy 思路）
#     上传方向：GitHub。需 GH_TOKEN + PROBE_FORK，失败仅记录。
if [[ "$TEST_SQUID_GIT_PUSH" == "1" && -n "${GH_TOKEN:-}" ]]; then
    log "== git push 上传链路 =="
    PROBE_FORK="${PROBE_FORK:-vllm-ascend-ci/vllm-ascend}"
    push_probe() {
        local d branch
        d=$(mktemp -d /tmp/test-squid-push.XXXXXX)
        branch="test-squid_probe_$(date +%s)"
        git -C "$d" init -q
        echo "test-squid probe" > "$d/f.txt"
        git -C "$d" add -f f.txt
        git -C "$d" -c user.name=test-squid -c user.email=ci@example.com commit -qm "probe"
        git -C "$d" push -q \
            "https://x-access-token:${GH_TOKEN}@github.com/${PROBE_FORK}.git" \
            "HEAD:refs/heads/${branch}"
        git -C "$d" push -q --delete \
            "https://x-access-token:${GH_TOKEN}@github.com/${PROBE_FORK}.git" \
            "$branch"
        rm -rf "$d"
    }
    run_timed git-push-probe upload push_probe
else
    log "[skip] TEST_SQUID_GIT_PUSH != 1 或无 GH_TOKEN，跳过 git push"
fi

# ---------------------------------------------------------------------------
# 8. 汇总
# ---------------------------------------------------------------------------
log "== 汇总 =="
cat "$TSV"
log "完成。结果目录: $RESULTS_DIR"
