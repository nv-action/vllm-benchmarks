#!/usr/bin/env bash
# Snappy 测试脚本:功能判定 + 收尾。
# 判定:源码归档 SHA-256 复核(source-integrity)、被测 snappy.pc 版本(version)、
# 链接被测库的 Compress/Uncompress 回环(roundtrip)。
# 某项失败后继续尝试其余检查,以便一次运行收集更多故障信息;
# 任一项失败,整体结果为 failed。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

log_line() {
    printf '%s\n' "$*" | tee -a "${RESULTS_DIR}/snappy_test.log"
}

# 复核 install 阶段保留的源码归档:tag 与 SHA-256 必须仍与 source-lock.json 一致。
# 归档丢失或被改动都会失败。
run_source_integrity() {
    [[ -f "${SNAPPY_ARCHIVE}" ]] || {
        log "ERROR: source archive is missing: ${SNAPPY_ARCHIVE}"
        return 1
    }
    python3 "${SCRIPT_DIR}/verify_source.py" snappy "${SOFTWARE_VERSION}" "${SNAPPY_ARCHIVE}"
}

# 被测交付物的版本证据:SnappyConfigVersion.cmake 的 PACKAGE_VERSION + 带版本号的库文件。
run_version() {
    [[ -f "${SNAPPY_LIB_FILE}" ]] || {
        log "ERROR: versioned library file is missing: ${SNAPPY_LIB_FILE}"
        return 1
    }
    [[ -f "${SNAPPY_VERSION_CMAKE}" ]] || {
        log "ERROR: SnappyConfigVersion.cmake is missing: ${SNAPPY_VERSION_CMAKE}"
        return 1
    }
    local actual
    actual="$(read_snappy_version "${SNAPPY_VERSION_CMAKE}")"
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: SnappyConfigVersion.cmake reports ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 1
    }
    printf 'SnappyConfigVersion.cmake PACKAGE_VERSION: %s\nversioned library: %s\n' \
        "${actual}" "${SNAPPY_LIB_FILE}"
}

# 编译并运行一个链接被测 libsnappy 的 C++ 程序,做 Compress/Uncompress 回环校验。
# prebuilt 模式不加 rpath:运行时必须经镜像配置的 ldconfig 解析 SONAME,
# 这正是参考 Dockerfile 运行层(/etc/ld.so.conf.d/snappy.conf + ldconfig)要验证的行为。
# build 模式的库装在私有目录,未进系统缓存,用 rpath 定位。
run_roundtrip() {
    local program="${HARNESS_DIR}/roundtrip.cc"
    local binary="${HARNESS_DIR}/roundtrip"
    mkdir -p "${HARNESS_DIR}"
    cat >"${program}" <<'CPP'
#include <snappy.h>
#include <cstdio>
#include <string>

int main() {
    std::string input;
    input.reserve(120000);
    for (int i = 0; i < 120000; ++i) {
        input.push_back(static_cast<char>((i * 31 + (i / 7)) % 251));
    }
    std::string compressed;
    snappy::Compress(input.data(), input.size(), &compressed);
    std::string output;
    if (!snappy::Uncompress(compressed.data(), compressed.size(), &output)) {
        std::fprintf(stderr, "uncompress failed\n");
        return 1;
    }
    if (output != input) {
        std::fprintf(stderr, "roundtrip mismatch\n");
        return 1;
    }
    std::printf("roundtrip ok: %zu bytes -> %zu bytes\n", input.size(), compressed.size());
    return 0;
}
CPP
    local -a rpath_args=()
    [[ "${FUNC_MODE}" == "prebuilt" ]] || rpath_args+=(-Wl,-rpath,"${SNAPPY_PREFIX}/${SNAPPY_LIBDIR}")
    c++ -std=c++11 -O2 -I"${SNAPPY_PREFIX}/include" \
        "${program}" -L"${SNAPPY_PREFIX}/${SNAPPY_LIBDIR}" -lsnappy \
        "${rpath_args[@]}" \
        -o "${binary}" || return 40
    "${binary}"
}

func_check() {
    initialize_runtime || return $?
    : >"${RESULTS_DIR}/snappy_test.log"
    : >"${RESULTS_DIR}/checks.tsv"
    local name function status overall=passed
    for name in source-integrity version roundtrip; do
        case "${name}" in
            source-integrity) function=run_source_integrity ;;
            version) function=run_version ;;
            roundtrip) function=run_roundtrip ;;
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
r = dict(software='snappy', category='HPC', version=os.environ['SOFTWARE_VERSION'],
         os=os.environ['FUNC_OS'], mode=os.environ['FUNC_MODE'], hardware=os.environ.get('FUNC_HARDWARE'),
         architecture=os.environ['EXPECTED_ARCH'], run_id=os.environ['PERF_RUN_ID'], status=sys.argv[2], checks=checks)
(out / 'functional_result.json').write_text(json.dumps(r, ensure_ascii=False, indent=2) + '\n')
RESULT
    [[ "${overall}" == passed ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${HARNESS_DIR}" ]] && rm -rf -- "${HARNESS_DIR}"
    log "cleaned snappy task data"
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
Run the snappy functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    snappy version (default: ${SOFTWARE_VERSION})
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
