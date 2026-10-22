#!/usr/bin/env bash
# cloudwego 测试脚本:功能判定 + 收尾。
# 判定:组件 CLI 版本、Kitex/Hertz 上游测试、HTTP 回环、代码生成和 Eino 测试。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/cloudwego_test.log"
}

# 运行 Kitex 与 Hertz 自带的上游单元测试。
run_upstream_tests() {
    [[ -f "${KITEX_SOURCE_DIR}/go.mod" && -f "${HERTZ_SOURCE_DIR}/go.mod" ]] || return 1
    (cd "${KITEX_SOURCE_DIR}" && "${GO_BIN}" test -count=1 -timeout 5m ./client/...) || return 1
    (cd "${HERTZ_SOURCE_DIR}" && "${GO_BIN}" test -count=1 -timeout 5m ./pkg/...) || return 1
}

# 用 Hertz 起一个最小 HTTP 服务,再用标准库 client 回环请求,校验响应内容。
# 程序放在 Hertz 源码树内,这样直接复用其模块上下文与已下载的依赖。
run_hertz_smoke() {
    local smoke_dir="${HERTZ_SOURCE_DIR}/functest_smoke"
    rm -rf -- "${smoke_dir}"
    mkdir -p "${smoke_dir}"
    cat >"${smoke_dir}/main.go" <<'GO'
package main

import (
	"context"
	"fmt"
	"io"
	"net/http"
	"os"
	"encoding/json"
	"time"
 "net"

	"github.com/cloudwego/hertz/pkg/app"
	"github.com/cloudwego/hertz/pkg/app/server"
	"github.com/cloudwego/hertz/pkg/common/utils"
	"github.com/cloudwego/hertz/pkg/protocol/consts"
)

func main() {
	listener, err := net.Listen("tcp", "127.0.0.1:0")
    if err != nil { panic(err) }
    addr := listener.Addr().String()
    listener.Close()
	h := server.Default(server.WithHostPorts(addr))
	h.GET("/ping", func(c context.Context, ctx *app.RequestContext) {
		ctx.JSON(consts.StatusOK, utils.H{"ping": "pong"})
	})
	go h.Spin()
	client := &http.Client{Timeout: 2 * time.Second}
    var resp *http.Response
    deadline := time.Now().Add(10 * time.Second)
    for time.Now().Before(deadline) {
        resp, err = client.Get("http://" + addr + "/ping")
        if err == nil { break }
        time.Sleep(100 * time.Millisecond)
    }
	if err != nil {
		fmt.Fprintln(os.Stderr, "request failed:", err)
		os.Exit(1)
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	if err != nil {
		fmt.Fprintln(os.Stderr, "read failed:", err)
		os.Exit(1)
	}
	var payload map[string]string
    if resp.StatusCode != http.StatusOK || json.Unmarshal(body, &payload) != nil || payload["ping"] != "pong" {
		fmt.Fprintf(os.Stderr, "unexpected response: status=%d body=%q\n", resp.StatusCode, string(body))
		os.Exit(1)
	}
	fmt.Printf("hertz roundtrip ok: %s\n", string(body))
}
GO
    (cd "${HERTZ_SOURCE_DIR}" && "${GO_BIN}" run ./functest_smoke)
}

run_codegen() {
    CODEGEN_DIR=""
    local dir
    dir="$(mktemp -d "${PERF_WORK_DIR}/codegen.XXXXXX")" || return 40
    mkdir -p "${dir}/kitex" "${dir}/hz" "${dir}/volo" || return 40
    cat >"${dir}/echo.thrift" <<'IDL'
namespace go echo
struct Request { 1: string message }
struct Response { 1: string message }
service Echo { Response Ping(1: Request req) (api.post="/ping") }
IDL
    (cd "${dir}/kitex" && "${KITEX_BIN}" -module cloudwego.local/echo ../echo.thrift) || return 40
    [[ -s "${dir}/kitex/kitex_gen/echo/echo.go" ]] || return 40
    (cd "${dir}/hz" && "${HZ_BIN}" new -mod cloudwego.local/http -idl ../echo.thrift) || return 40
    [[ -s "${dir}/hz/main.go" ]] || return 40
    (cd "${dir}/volo" && "${VOLO_BIN}" init echo ../echo.thrift) || return 40
    [[ -s "${dir}/volo/Cargo.toml" && -s "${dir}/volo/src/lib.rs" ]] || return 40
    CODEGEN_DIR="${dir}"
    printf '%s\n' "${dir}" >"${RESULTS_DIR}/codegen-dir.txt"
}

run_source_integrity() {
    local component tag
    for component in kitex hertz volo eino; do
        case "${component}" in
            kitex) tag="v${SOFTWARE_VERSION}" ;;
            hertz) tag="v${CLOUDWEGO_HERTZ_VERSION}" ;;
            volo) tag="volo-thrift-${CLOUDWEGO_VOLO_VERSION}" ;;
            eino) tag="v${CLOUDWEGO_EINO_VERSION}" ;;
        esac
        python3 "${SCRIPT_DIR}/verify_source.py" "${component}" "${tag}" "${PERF_WORK_DIR}/${component}.tar.gz" || return 40
    done
}

run_generated_build() {
    [[ -n "${CODEGEN_DIR:-}" ]] || { log "ERROR: code generation did not succeed"; return 40; }
    local component module version source
    for component in kitex hz; do
        if [[ "${component}" == kitex ]]; then
            module=github.com/cloudwego/kitex; version="${SOFTWARE_VERSION}"; source="${KITEX_SOURCE_DIR}"
        else
            module=github.com/cloudwego/hertz; version="${CLOUDWEGO_HERTZ_VERSION}"; source="${HERTZ_SOURCE_DIR}"
        fi
        (
            cd "${CODEGEN_DIR}/${component}" || exit 40
            "${GO_BIN}" mod edit "-require=${module}@v${version}" "-replace=${module}=${source}" || exit 40
            "${GO_BIN}" mod tidy || exit 40
            "${GO_BIN}" build -mod=readonly ./... || exit 40
            cp go.mod "${RESULTS_DIR}/generated-${component}.mod" || exit 40
            cp go.sum "${RESULTS_DIR}/generated-${component}.sum" || exit 40
        ) || return 40
    done
    python3 "${SCRIPT_DIR}/pin_generated.py" "${CODEGEN_DIR}/volo" "${VOLO_SOURCE_DIR}" || return 40
    (
        cd "${CODEGEN_DIR}/volo" || exit 40
        # 新项目需要更新锁文件以加入生成包；框架源和直接依赖均固定。
        cargo metadata --filter-platform "$(rustc -vV | sed -n 's/^host: //p')" --format-version 1 >"${RESULTS_DIR}/generated-cargo-metadata.json" || exit 40
        cp Cargo.lock "${RESULTS_DIR}/generated-Cargo.lock" || exit 40
        cargo build --locked
    ) || return 40
}

run_kitex_rpc() {
    [[ -n "${CODEGEN_DIR:-}" ]] || { log "ERROR: code generation did not succeed"; return 40; }
    cp "${SCRIPT_DIR}/kitex_rpc_test.go" "${CODEGEN_DIR}/kitex/rpc_test.go" || return 40
    (cd "${CODEGEN_DIR}/kitex" && "${GO_BIN}" mod tidy && "${GO_BIN}" test -mod=readonly -count=1 -timeout 30s -v -run '^TestRPCRoundtrip$' .)
}

run_eino_tests() {
    (cd "${EINO_SOURCE_DIR}" && "${GO_BIN}" test -count=1 -timeout 5m ./schema/... ./components/prompt/...)
}

func_check() {
    initialize_runtime || return $?
    : >"${RESULTS_DIR}/cloudwego_test.log"
    : >"${RESULTS_DIR}/checks.tsv"
    local name function status overall=passed
    CODEGEN_DIR=""
    for name in source-integrity version upstream-test smoke codegen generated-build kitex-rpc eino-test; do
        case "${name}" in
            source-integrity) function=run_source_integrity ;;
            generated-build) function=run_generated_build ;;
            kitex-rpc) function=run_kitex_rpc ;;
            version) function=verify_components ;;
            upstream-test) function=run_upstream_tests ;;
            smoke) function=run_hertz_smoke ;;
            codegen) function=run_codegen ;;
            eino-test) function=run_eino_tests ;;
        esac
        if "${function}" >"${RESULTS_DIR}/${name}.log" 2>&1; then
            status=passed
        else
            status=failed
            overall=failed
        fi
        printf '%s\t%s\tsee %s.log\n' "${name}" "${status}" "${name}" >>"${RESULTS_DIR}/checks.tsv"
        log_line "${name}: ${status}"
    done
    python3 - "${RESULTS_DIR}" "${overall}" <<'RESULT' || return 40
import json, os, sys
from pathlib import Path
out = Path(sys.argv[1])
checks = []
for line in (out / 'checks.tsv').read_text().splitlines():
    name, status, detail = line.split('\t')
    checks.append(dict(name=name, status=status, detail=detail))
r = dict(software='cloudwego', category='Middleware', version=os.environ['SOFTWARE_VERSION'],
         os=os.environ['FUNC_OS'], mode=os.environ['FUNC_MODE'], hardware=os.environ.get('FUNC_HARDWARE'),
         architecture=os.environ['EXPECTED_ARCH'], run_id=os.environ['PERF_RUN_ID'], status=sys.argv[2], checks=checks)
(out / 'functional_result.json').write_text(json.dumps(r, indent=2) + '\n')
RESULT
    [[ "${overall}" == passed ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${HERTZ_SOURCE_DIR}/functest_smoke" ]] && rm -rf -- "${HERTZ_SOURCE_DIR}/functest_smoke"
    local generated
    for generated in "${PERF_WORK_DIR}"/codegen.*; do
        [[ ! -d "${generated}" ]] || rm -rf -- "${generated}" || return 70
    done
    log "cleaned cloudwego task data"
}

# ---------- 独立运行入口 ----------

cleanup_standalone_workdir() {
    if [[ "${STANDALONE_KEEP_WORK_DIR}" -eq 1 ]]; then
        log "keeping work directory: ${PERF_WORK_DIR}"
        return 0
    fi
    [[ "${STANDALONE_OWNS_WORK_DIR}" -eq 1 ]] || return 0
    [[ "${PERF_WORK_DIR}" == /home/runner/boostkit-func/${SOFTWARE_NAME}/local-* ]] || {
        log "ERROR: refusing to remove unexpected work directory: ${PERF_WORK_DIR}"
        return 70
    }
    [[ -d "${PERF_WORK_DIR}" ]] && rm -rf -- "${PERF_WORK_DIR}"
    log "removed standalone work directory: ${PERF_WORK_DIR}"
}

emergency_cleanup() {
    set +e
    func_cleanup
    cleanup_standalone_workdir
}

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Run the CloudWeGo functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    CloudWeGo (Kitex) version (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --mode MODE          prebuilt | build (default: ${FUNC_MODE})
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_test_standalone() {
    local stage_status=0 failed_stage="" finalize_status=0

    configure_runtime_paths || return $?
    trap emergency_cleanup EXIT
    initialize_runtime || return $?

    func_check || {
        stage_status=$?
        failed_stage="check"
    }
    func_cleanup || true
    cleanup_standalone_workdir || finalize_status=70
    trap - EXIT

    log "standalone results: ${RESULTS_DIR}"
    if [[ "${stage_status}" -ne 0 ]]; then
        log "standalone failed at stage: ${failed_stage}"
        return "${stage_status}"
    fi
    return "${finalize_status}"
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
    mkdir -p "${RESULTS_DIR:-${SCRIPT_DIR}/results}" || return $?
    run_test_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi
