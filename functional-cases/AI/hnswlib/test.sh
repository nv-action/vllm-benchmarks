#!/usr/bin/env bash
# hnswlib 测试脚本:功能判定 + 收尾。
# 判定:头文件聚合指纹的版本校验 + g++ 编译运行 add/search 回环 + saveIndex/reload 回环。
# 暴露给 Framework 的阶段:check、cleanup。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:check ----------

# 编译探针:用 g++ 编译并运行上游 HNSW 核心 API(addPoint/searchKnn/saveIndex)。
# 头文件以 <hnswlib/hnswlib.h> 引用,所以 -I 指向头文件目录的父目录。
write_knn_roundtrip() {
    cat >"$1" <<'CPP'
#include <hnswlib/hnswlib.h>

#include <cstdio>
#include <queue>
#include <utility>
#include <vector>

int main() {
    const size_t dim = 16;
    const size_t num_elements = 200;
    const size_t M = 16;
    const size_t ef_construction = 200;

    hnswlib::L2Space space(dim);
    hnswlib::HierarchicalNSW<float> *index =
        new hnswlib::HierarchicalNSW<float>(&space, num_elements, M, ef_construction);
    index->setEf(num_elements);

    std::vector<float> data(dim * num_elements, 0.0f);
    for (size_t i = 0; i < num_elements; ++i) {
        data[i * dim] = static_cast<float>(i);
        index->addPoint(data.data() + i * dim, static_cast<hnswlib::labeltype>(i));
    }

    // 自查询:每个点检索最近的 1 个,应当命中自身(高 ef 下应全中)。
    size_t hits = 0;
    for (size_t i = 0; i < num_elements; ++i) {
        std::priority_queue<std::pair<float, hnswlib::labeltype> > result =
            index->searchKnn(data.data() + i * dim, 1);
        if (!result.empty() && static_cast<size_t>(result.top().second) == i) {
            ++hits;
        }
    }
    const double recall =
        static_cast<double>(hits) / static_cast<double>(num_elements);
    std::printf("self_recall=%.3f\n", recall);
    if (hits != num_elements) {
        std::fprintf(stderr, "FAIL: self query missed %zu of %zu\n",
                     num_elements - hits, num_elements);
        delete index;
        return 1;
    }

    // 插值查询:第 0 维取 150.25,最近邻应为标签 150。
    std::vector<float> query(dim, 0.0f);
    query[0] = 150.25f;
    std::priority_queue<std::pair<float, hnswlib::labeltype> > nearest =
        index->searchKnn(query.data(), 1);
    if (nearest.empty() || static_cast<size_t>(nearest.top().second) != 150) {
        std::fprintf(stderr, "FAIL: interpolated query did not return label 150\n");
        delete index;
        return 1;
    }
    std::printf("interp_label=%zu\n", static_cast<size_t>(nearest.top().second));

    delete index;
    std::printf("HNSWLIB_KNN_OK\n");
    return 0;
}
CPP
}

# 落盘回环:saveIndex 后重新加载索引,校验加载后的检索结果一致。
write_save_reload() {
    cat >"$1" <<'CPP'
#include <hnswlib/hnswlib.h>

#include <cstdio>
#include <queue>
#include <string>
#include <utility>
#include <vector>

int main(int argc, char **argv) {
    if (argc != 2) {
        std::fprintf(stderr, "usage: %s <index-path>\n", argv[0]);
        return 2;
    }
    const std::string index_path = argv[1];
    const size_t dim = 16;
    const size_t num_elements = 200;
    const size_t M = 16;
    const size_t ef_construction = 200;

    hnswlib::L2Space space(dim);
    std::vector<float> data(dim * num_elements, 0.0f);
    for (size_t i = 0; i < num_elements; ++i) {
        data[i * dim] = static_cast<float>(i);
    }

    {
        hnswlib::HierarchicalNSW<float> *index =
            new hnswlib::HierarchicalNSW<float>(&space, num_elements, M, ef_construction);
        index->setEf(num_elements);
        for (size_t i = 0; i < num_elements; ++i) {
            index->addPoint(data.data() + i * dim, static_cast<hnswlib::labeltype>(i));
        }
        index->saveIndex(index_path);
        delete index;
    }

    hnswlib::HierarchicalNSW<float> *reloaded =
        new hnswlib::HierarchicalNSW<float>(&space, index_path);
    reloaded->setEf(num_elements);

    std::vector<float> query(dim, 0.0f);
    query[0] = 42.4f;
    std::priority_queue<std::pair<float, hnswlib::labeltype> > result =
        reloaded->searchKnn(query.data(), 1);
    if (result.empty() || static_cast<size_t>(result.top().second) != 42) {
        std::fprintf(stderr, "FAIL: reloaded index did not return label 42\n");
        delete reloaded;
        return 1;
    }
    std::printf("reload_label=%zu\n", static_cast<size_t>(result.top().second));
    delete reloaded;

    std::printf("HNSWLIB_RELOAD_OK\n");
    return 0;
}
CPP
}

write_functional_result() {
    local overall="$1"
    shift
    FUNC_OVERALL="${overall}" python3 - "${RESULTS_DIR}/functional_result.json" "$@" <<'PY'
import json, os, sys

out = sys.argv[1]
items = sys.argv[2:]
env = os.environ.get
checks = [
    {"name": items[i], "status": items[i + 1], "detail": items[i + 2]}
    for i in range(0, len(items), 3)
]
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
    "checks": checks,
}
tmp = out + ".tmp"
with open(tmp, "w", encoding="utf-8") as handle:
    handle.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
os.replace(tmp, out)
PY
}

func_check() {
    initialize_runtime || return $?
    # 编译探针需要 g++ 与 sha256sum(build 模式已在 install 阶段装好;prebuilt 镜像自带)。
    require_check_commands || return $?
    [[ -n "${HNSWLIB_INCLUDE_DIR}" && -d "${HNSWLIB_INCLUDE_DIR}" ]] || {
        log "ERROR: hnswlib headers are unavailable: ${HNSWLIB_INCLUDE_DIR:-unset}"
        return 40
    }
    local log_file="${RESULTS_DIR}/functional_test.log"
    local include_root
    include_root="$(dirname "${HNSWLIB_INCLUDE_DIR}")"
    : >"${log_file}"

    local version_status="failed" version_detail="" actual
    local knn_status="failed" knn_detail=""
    local reload_status="failed" reload_detail=""

    # 1) 版本:整套头文件的聚合指纹
    actual="$(read_installed_version)"
    if [[ "${actual}" == "${SOFTWARE_VERSION}" ]]; then
        version_status="passed"
        version_detail="hnswlib ${actual} (header fingerprint matched)"
    else
        version_detail="hnswlib ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        log_headers_diagnostic "${HNSWLIB_INCLUDE_DIR:-}" >>"${log_file}" 2>&1
    fi
    printf 'version: %s (%s)\n' "${version_status}" "${version_detail}" >>"${log_file}"

    # 2) g++ 编译并运行 add/search 回环
    if write_knn_roundtrip "${TMPDIR}/hnswlib_knn.cpp" &&
        "${CXX}" -std=c++11 -O2 -pthread -I"${include_root}" \
            "${TMPDIR}/hnswlib_knn.cpp" -o "${TMPDIR}/hnswlib_knn" >>"${log_file}" 2>&1 &&
        "${TMPDIR}/hnswlib_knn" >>"${log_file}" 2>&1; then
        knn_status="passed"
        knn_detail="addPoint/searchKnn roundtrip returned exact self labels and the interpolated neighbour"
    else
        knn_detail="knn roundtrip failed; see functional_test.log"
    fi
    printf 'hnsw-knn-roundtrip: %s (%s)\n' "${knn_status}" "${knn_detail}" >>"${log_file}"

    # 3) saveIndex 落盘后重新加载
    if write_save_reload "${TMPDIR}/hnswlib_reload.cpp" &&
        "${CXX}" -std=c++11 -O2 -pthread -I"${include_root}" \
            "${TMPDIR}/hnswlib_reload.cpp" -o "${TMPDIR}/hnswlib_reload" >>"${log_file}" 2>&1 &&
        "${TMPDIR}/hnswlib_reload" "${TMPDIR}/hnswlib_index.bin" >>"${log_file}" 2>&1; then
        reload_status="passed"
        reload_detail="saveIndex + reload returned the same neighbour"
    else
        reload_detail="save/reload roundtrip failed; see functional_test.log"
    fi
    printf 'hnsw-save-reload: %s (%s)\n' "${reload_status}" "${reload_detail}" >>"${log_file}"

    local overall="passed"
    if [[ "${version_status}" != "passed" || "${knn_status}" != "passed" || "${reload_status}" != "passed" ]]; then
        overall="failed"
    fi
    write_functional_result "${overall}" \
        "version" "${version_status}" "${version_detail}" \
        "hnsw-knn-roundtrip" "${knn_status}" "${knn_detail}" \
        "hnsw-save-reload" "${reload_status}" "${reload_detail}"
    log "checks: version=${version_status} hnsw-knn-roundtrip=${knn_status} hnsw-save-reload=${reload_status}"

    [[ "${overall}" == "passed" ]] || return 50
}

# ---------- 阶段:cleanup ----------

func_cleanup() {
    initialize_runtime || return $?
    [[ -d "${BUILD_DIR}" ]] && rm -rf -- "${BUILD_DIR}"
    [[ -d "${TMPDIR}" ]] && rm -rf -- \
        "${TMPDIR}/hnswlib_knn.cpp" "${TMPDIR}/hnswlib_knn" \
        "${TMPDIR}/hnswlib_reload.cpp" "${TMPDIR}/hnswlib_reload" \
        "${TMPDIR}/hnswlib_index.bin"
    log "cleaned hnswlib task data"
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
Run the hnswlib (C++ headers) functional checks and clean up for one OS/hardware cell.

Options:
  --version VERSION    hnswlib version (default: ${SOFTWARE_VERSION})
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
