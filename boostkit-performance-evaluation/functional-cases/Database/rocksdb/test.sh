#!/usr/bin/env bash
# RocksDB 测试脚本:功能判定 + 收尾。
# 判定采用:版本校验 + 上游 db_bench 工具烟测 + 自编译最小 C++ API 烟测。
# RocksDB 是进程内嵌入式库,无服务端,故无 service 启停,cleanup 仅清数据目录。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 上游 db_bench:先写满,再只读遍历,验证官方工具可正常读写。
# db_bench 可能依赖镜像内非标准路径的共享库,统一用 LD_LIBRARY_PATH 补全。
run_upstream_test() {
    local data_dir="${PERF_WORK_DIR}/db_bench_data" lib_path
    lib_path="$(rocksdb_runtime_lib_path)"
    rm -rf -- "${data_dir}"
    mkdir -p "${data_dir}"
    log "db_bench fillrandom --num=${ROCKSDB_BENCH_NUM}"
    LD_LIBRARY_PATH="${lib_path}${LD_LIBRARY_PATH:-}" "${ROCKSDB_DB_BENCH_BIN}" \
        --db="${data_dir}" \
        --benchmarks=fillrandom \
        --num="${ROCKSDB_BENCH_NUM}" \
        --value_size=100 \
        --threads=1 \
        --statistics=0 || return $?
    log "db_bench readrandom (read-only smoke)"
    LD_LIBRARY_PATH="${lib_path}${LD_LIBRARY_PATH:-}" "${ROCKSDB_DB_BENCH_BIN}" \
        --db="${data_dir}" \
        --benchmarks=readrandom,readmissing \
        --num="${ROCKSDB_BENCH_NUM}" \
        --value_size=100 \
        --use_existing_db=1 \
        --threads=1 \
        --statistics=0 || return $?
    rm -rf -- "${data_dir}"
}

# db_bench 启动失败时把动态库解析情况记进日志,便于区分“镜像缺库”与“库不在加载路径”。
log_db_bench_diagnostics() {
    {
        printf -- '--- ldd %s ---\n' "${ROCKSDB_DB_BENCH_BIN}"
        LD_LIBRARY_PATH="$(rocksdb_runtime_lib_path)" ldd "${ROCKSDB_DB_BENCH_BIN}" 2>&1 || true
        printf -- '--- librocksdb* found on disk ---\n'
        find /usr /usr/local /opt /lib /lib64 -maxdepth 5 -name 'librocksdb*' -print 2>/dev/null | head -n 20 || true
    } >>"${RESULTS_DIR}/upstream_test.log"
}

# 基础功能 smoke:编译并运行一个最小的 C++ 程序,验证打开/写入/读取/重开。
# rocksdb 11.x 起头文件要求 C++20,且 DB::Open 只保留 std::unique_ptr<DB> 重载,
# 故按 version.h 的 ROCKSDB_MAJOR 分支选择 API。
api_smoke_source() {
    cat <<'CPP'
#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/version.h>
#include <iostream>
#include <memory>
#include <string>

int main(int argc, char** argv) {
    const std::string path = (argc > 1) ? argv[1] : "./rocksdb_smoke_db";
    rocksdb::Options options;
    options.create_if_missing = true;
#if ROCKSDB_MAJOR >= 11
    std::unique_ptr<rocksdb::DB> db;
    rocksdb::Status status = rocksdb::DB::Open(options, path, &db);
    if (!status.ok()) { std::cerr << "open: " << status.ToString() << "\n"; return 1; }
    status = db->Put(rocksdb::WriteOptions(), "func_key", "func_value");
    if (!status.ok()) { std::cerr << "put: " << status.ToString() << "\n"; return 1; }
    std::string value;
    status = db->Get(rocksdb::ReadOptions(), "func_key", &value);
    if (!status.ok() || value != "func_value") { std::cerr << "get mismatch\n"; return 1; }
    db.reset();
    status = rocksdb::DB::Open(options, path, &db);
    if (!status.ok()) { std::cerr << "reopen: " << status.ToString() << "\n"; return 1; }
    value.clear();
    status = db->Get(rocksdb::ReadOptions(), "func_key", &value);
    if (!status.ok() || value != "func_value") { std::cerr << "reopen get mismatch\n"; return 1; }
    db.reset();
#else
    rocksdb::DB* db = nullptr;
    rocksdb::Status status = rocksdb::DB::Open(options, path, &db);
    if (!status.ok()) { std::cerr << "open: " << status.ToString() << "\n"; return 1; }
    status = db->Put(rocksdb::WriteOptions(), "func_key", "func_value");
    if (!status.ok()) { std::cerr << "put: " << status.ToString() << "\n"; return 1; }
    std::string value;
    status = db->Get(rocksdb::ReadOptions(), "func_key", &value);
    if (!status.ok() || value != "func_value") { std::cerr << "get mismatch\n"; return 1; }
    delete db;
    status = rocksdb::DB::Open(options, path, &db);
    if (!status.ok()) { std::cerr << "reopen: " << status.ToString() << "\n"; return 1; }
    value.clear();
    status = db->Get(rocksdb::ReadOptions(), "func_key", &value);
    if (!status.ok() || value != "func_value") { std::cerr << "reopen get mismatch\n"; return 1; }
    delete db;
#endif
    std::cout << "api-smoke ok\n";
    return 0;
}
CPP
}

# 头文件要求的 C++ 标准:11.x 起必须 C++20。
rocksdb_cxx_std() {
    local version major
    version="$(rocksdb_version_from_header "${ROCKSDB_INCLUDE_DIR}/rocksdb/version.h" || true)"
    major="${version%%.*}"
    if [[ "${major}" =~ ^[0-9]+$ ]] && (( major >= 11 )); then
        printf '%s\n' "-std=c++20"
    else
        printf '%s\n' "-std=c++17"
    fi
}

# 依次尝试静态库(全套可选依赖)、动态库、静态库(精简依赖)三种链接方式。
compile_api_smoke() {
    local src="$1" out="$2" log="$3"
    local std
    std="$(rocksdb_cxx_std)"
    if g++ "${std}" -I"${ROCKSDB_INCLUDE_DIR}" "${src}" -o "${out}" \
        "${ROCKSDB_LIB_DIR}/librocksdb.a" -lz -lbz2 -lsnappy -llz4 -lzstd -lpthread -ldl >>"${log}" 2>&1; then
        return 0
    fi
    if g++ "${std}" -I"${ROCKSDB_INCLUDE_DIR}" "${src}" -o "${out}" \
        -L"${ROCKSDB_LIB_DIR}" -lrocksdb -lz -lpthread -ldl >>"${log}" 2>&1; then
        return 0
    fi
    if g++ "${std}" -I"${ROCKSDB_INCLUDE_DIR}" "${src}" -o "${out}" \
        "${ROCKSDB_LIB_DIR}/librocksdb.a" -lz -lpthread -ldl >>"${log}" 2>&1; then
        return 0
    fi
    return 1
}

run_api_smoke() {
    local work="${PERF_WORK_DIR}/api-smoke"
    local src="${work}/smoke.cc"
    local bin="${work}/smoke"
    local db_dir="${work}/smoke_db"
    local lib_path
    lib_path="$(rocksdb_runtime_lib_path)"
    rm -rf -- "${work}"
    mkdir -p "${work}"
    api_smoke_source >"${src}"
    : >"${RESULTS_DIR}/api_smoke.log"
    compile_api_smoke "${src}" "${bin}" "${RESULTS_DIR}/api_smoke.log" || return $?
    # 静态链接时不需要,但镜像只提供动态库时(常见)必须补 LD_LIBRARY_PATH。
    LD_LIBRARY_PATH="${lib_path}${LD_LIBRARY_PATH:-}" "${bin}" "${db_dir}" || return $?
}

# api-smoke 需要就地编译并链接 rocksdb 及其可选压缩库;prebuilt 模式的软件镜像通常只装了
# 运行时库、没有编译器和 -devel 包,缺失时按发行版补装。
ensure_smoke_toolchain() {
    if ! command -v g++ >/dev/null 2>&1; then
        log "g++ not found; installing build tools for the API smoke test"
        install_os_packages build-tools || return 30
    fi
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        ensure_rocksdb_runtime_deps || return $?
    fi
    command -v g++ >/dev/null 2>&1
}

write_functional_result() {
    local overall="$1" v_status="$2" v_detail="$3" u_status="$4" u_detail="$5" s_status="$6" s_detail="$7"
    FUNC_OVERALL="${overall}" \
        FUNC_V_STATUS="${v_status}" FUNC_V_DETAIL="${v_detail}" \
        FUNC_U_STATUS="${u_status}" FUNC_U_DETAIL="${u_detail}" \
        FUNC_S_STATUS="${s_status}" FUNC_S_DETAIL="${s_detail}" \
        python3 - "${RESULTS_DIR}/functional_result.json" <<'PY'
import json, os, sys

out = sys.argv[1]
env = os.environ.get
payload = {
    "software": env("SOFTWARE_NAME"),
    "category": env("CATEGORY"),
    "version": env("SOFTWARE_VERSION"),
    "os": env("FUNC_OS"),
    "mode": env("FUNC_MODE"),
    "hardware": env("FUNC_HARDWARE") or None,
    "architecture": env("EXPECTED_ARCH"),
    "run_id": env("PERF_RUN_ID"),
    "status": env("FUNC_OVERALL"),
    "checks": [
        {"name": "version", "status": env("FUNC_V_STATUS"), "detail": env("FUNC_V_DETAIL")},
        {"name": "upstream-test", "status": env("FUNC_U_STATUS"), "detail": env("FUNC_U_DETAIL")},
        {"name": "api-smoke", "status": env("FUNC_S_STATUS"), "detail": env("FUNC_S_DETAIL")},
    ],
}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, out)
PY
}

func_check() {
    initialize_runtime || return $?
    [[ -n "${ROCKSDB_DB_BENCH_BIN}" && -x "${ROCKSDB_DB_BENCH_BIN}" ]] || {
        log "ERROR: db_bench is unavailable; did the install stage run?"
        return 40
    }

    local version_status="failed" version_detail=""
    local upstream_status="failed" upstream_detail=""
    local smoke_status="failed" smoke_detail=""
    local actual

    # 1) 版本
    actual="$(rocksdb_version_from_header "${ROCKSDB_INCLUDE_DIR}/rocksdb/version.h" || true)"
    if [[ "${actual}" == "${SOFTWARE_VERSION#v}" ]]; then
        version_status="passed"
        version_detail="rocksdb ${actual} (version.h)"
    else
        version_detail="rocksdb ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
    fi

    # 2) 上游 db_bench 工具
    # 预构建镜像可能漏打 db_bench 的间接运行时库(如 libatomic),先按实际链接情况补齐。
    ensure_db_bench_runtime_libs || log "WARN: could not satisfy db_bench runtime dependencies"
    if run_upstream_test >"${RESULTS_DIR}/upstream_test.log" 2>&1; then
        upstream_status="passed"
        upstream_detail="upstream db_bench fillrandom/readrandom ok (num=${ROCKSDB_BENCH_NUM})"
    else
        upstream_detail="upstream db_bench smoke failed; see upstream_test.log"
        log_db_bench_diagnostics
    fi

    # 3) 自编译 API 烟测
    if ! ensure_smoke_toolchain; then
        smoke_detail="C++ compiler is unavailable for the API smoke test"
    elif run_api_smoke >"${RESULTS_DIR}/api_smoke.log" 2>&1; then
        smoke_status="passed"
        smoke_detail="compiled C++ smoke: Open/Put/Get/reopen ok"
    else
        smoke_detail="C++ API smoke failed; see api_smoke.log"
    fi

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${upstream_status}" != "passed" || "${smoke_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "${version_status}" "${version_detail}" \
        "${upstream_status}" "${upstream_detail}" \
        "${smoke_status}" "${smoke_detail}"
    log "checks: version=${version_status} upstream=${upstream_status} api-smoke=${smoke_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${ROCKSDB_BENCH_DATA}" ]] && rm -rf -- "${ROCKSDB_BENCH_DATA}"
    [[ -d "${PERF_WORK_DIR}/api-smoke" ]] && rm -rf -- "${PERF_WORK_DIR}/api-smoke"
    [[ -d "${PERF_WORK_DIR}/db_bench_data" ]] && rm -rf -- "${PERF_WORK_DIR}/db_bench_data"
    log "cleaned rocksdb task data"
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
Run the RocksDB functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    RocksDB version (default: ${SOFTWARE_VERSION})
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