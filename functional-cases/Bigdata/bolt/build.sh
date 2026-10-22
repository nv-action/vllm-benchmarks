#!/usr/bin/env bash
# Bolt 构建脚本:环境采集 + 准备被测软件(自源码用 Conan+CMake 构建,或校验镜像内已有构建)。
#   FUNC_MODE=prebuilt - 校验镜像内已有 Bolt 构建(无官方预编译产物,优先复用镜像)
#   FUNC_MODE=build    - 分层获取:1) 上游官方二进制 2) 三方仓库包 3) 源码编译(Conan+CMake,很重)
#                        Bolt 官方仅有源码,层1/层2 无可用渠道,只能走层3。
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version cmake_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cmake_version="$(cmake --version 2>/dev/null | head -n 1 || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")",
  "cmake_version": "$(json_escape "${cmake_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# Bolt 用 Conan 2 管理依赖;缺失时通过 pip 安装到用户目录。
ensure_conan() {
    if command -v conan >/dev/null 2>&1; then
        return 0
    fi
    log "installing Conan 2 via pip"
    python3 -m pip install --upgrade --user -i https://pypi.tuna.tsinghua.edu.cn/simple conan ||
        python3 -m pip install --upgrade --break-system-packages -i https://pypi.tuna.tsinghua.edu.cn/simple conan || {
        log "ERROR: failed to install Conan"
        return 30
    }
    export PATH="${HOME}/.local/bin:${PATH}"
    command -v conan >/dev/null 2>&1 || {
        log "ERROR: conan is unavailable after installation"
        return 30
    }
}

# 通过 GitHub API 把短 sha 解析为完整 40 位 sha(git fetch 服务端只接受完整对象名;
# 实测短 sha 直接 fetch 报 "couldn't find remote ref")。输入已是完整 sha 时原样返回。
# 注意:本函数经命令替换调用,内部日志必须写 stderr,否则会污染捕获结果
# (实测曾把日志行连同 sha 一起传给 git fetch,报 invalid refspec)。
resolve_full_commit() {
    local query="${1}" repo=""
    if [[ "${query}" =~ ^[0-9a-fA-F]{40}$ ]]; then
        printf '%s\n' "${query,,}"
        return 0
    fi
    if [[ "${BOLT_SOURCE_URL}" =~ github\.com[:/]+([A-Za-z0-9._-]+/[A-Za-z0-9._-]+) ]]; then
        repo="${BASH_REMATCH[1]%.git}"
    else
        log "ERROR: cannot resolve a commit ref without a github.com source URL: ${BOLT_SOURCE_URL}" >&2
        return 1
    fi
    log "resolving commit ${query} via GitHub API (repo: ${repo})" >&2
    https_proxy="${PERF_PROXY:-${https_proxy:-}}" python3 - "${repo}" "${query}" <<'PY'
import json, sys, urllib.request

repo, query = sys.argv[1], sys.argv[2]
req = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/commits/{query}",
    headers={"Accept": "application/vnd.github+json"},
)
with urllib.request.urlopen(req, timeout=30) as resp:
    print(json.load(resp)["sha"])
PY
}

clone_bolt_source() {
    command -v git >/dev/null 2>&1 || {
        log "ERROR: git is required to fetch the Bolt source"
        return 40
    }
    [[ -n "${BOLT_SOURCE_DIR}" && ! -e "${BOLT_SOURCE_DIR}" ]] || {
        log "ERROR: source directory is not clean: ${BOLT_SOURCE_DIR}"
        return 20
    }
    log "cloning Bolt ${SOFTWARE_VERSION} from ${BOLT_SOURCE_URL}"
    # 路径 1:分支 / tag 直接浅克隆(如 standalone 传 main)。
    if GIT_TERMINAL_PROMPT=0 git clone --branch "${SOFTWARE_VERSION}" --depth 1 \
        "${BOLT_SOURCE_URL}" "${BOLT_SOURCE_DIR}"; then
        return 0
    fi
    # 路径 2:commit(--branch 不接受 sha)。解析完整 sha 后按对象浅抓取,detached 检出。
    rm -rf -- "${BOLT_SOURCE_DIR}"
    local full_sha
    full_sha="$(resolve_full_commit "${SOFTWARE_VERSION}")" || {
        log "ERROR: cannot resolve Bolt ref ${SOFTWARE_VERSION} to a full commit sha"
        return 30
    }
    log "fetching Bolt commit ${full_sha}"
    GIT_TERMINAL_PROMPT=0 git init -q "${BOLT_SOURCE_DIR}" &&
        git -C "${BOLT_SOURCE_DIR}" remote add origin "${BOLT_SOURCE_URL}" &&
        GIT_TERMINAL_PROMPT=0 git -C "${BOLT_SOURCE_DIR}" fetch --depth 1 origin "${full_sha}" &&
        git -C "${BOLT_SOURCE_DIR}" checkout --detach --quiet FETCH_HEAD || {
        rm -rf -- "${BOLT_SOURCE_DIR}"
        log "ERROR: failed to fetch Bolt commit ${full_sha}"
        return 30
    }
}

# 生成 Conan 默认 profile 并按上游要求配置。
# 对齐上游 scripts/setup-dev-env.sh 的 check_conan:默认 profile 不存在时
# conan profile detect(自动探测编译器,同时生成 settings.yml)→ 确保
# compiler.cppstd=gnu17(上游 CPP_STANDARD)→ 跑上游脚本补充 os.libc/os.libc_version
# settings(scripts/configure-conan-libc-settings.py,需 PyYAML,随 conan 一并安装)。
# 注意:此前误用不存在的 scripts/configure-conan-profile.py,实测报 file not found。
configure_conan_profile() {
    local conan_home profile cppstd="gnu17"
    conan_home="${CONAN_HOME:-${HOME}/.conan2}"
    profile="${conan_home}/profiles/default"
    if [[ ! -f "${profile}" ]]; then
        conan profile detect || {
            log "ERROR: conan profile detect failed"
            return 40
        }
    fi
    [[ -f "${profile}" ]] || {
        log "ERROR: conan default profile was not created at ${profile}"
        return 40
    }
    if grep -q "^compiler.cppstd=" "${profile}"; then
        sed -i "s/^compiler.cppstd=.*/compiler.cppstd=${cppstd}/" "${profile}"
    else
        printf 'compiler.cppstd=%s\n' "${cppstd}" >>"${profile}"
    fi
    log "conan profile ready: ${profile} (compiler.cppstd=${cppstd})"
    (cd "${BOLT_SOURCE_DIR}" && python3 ./scripts/configure-conan-libc-settings.py) || {
        log "ERROR: failed to configure conan libc settings"
        return 40
    }
}

build_bolt() {
    local -a make_args=(BUILD_VERSION="${SOFTWARE_VERSION}")
    configure_conan_profile || return $?
    # 上游官方目标 release_with_test = release 的全部选项 + skip_test=False +
    # enable_testutil=True,使测试二进制真实编译(上游 unittest_release 即此目标 + ctest)。
    # 普通 make release 不编译测试(BOLT_BUILD_TESTING 默认 OFF),ctest 会零测试空跑。
    # 首次构建会用 Conan 从源码编译依赖,耗时较长(见 timeout_minutes)。
    (cd "${BOLT_SOURCE_DIR}" && make release_with_test "${make_args[@]}") || {
        log "ERROR: Bolt build failed"
        return 40
    }
    [[ -d "${BOLT_BUILD_DIR}" ]] || {
        log "ERROR: expected Bolt build directory was not produced: ${BOLT_BUILD_DIR}"
        return 40
    }
}

# ---------- 分层获取(build 模式) ----------
# 优先级:1) 上游官方二进制  2) 三方仓库打包的二进制/包/镜像  3) 源码编译。
# Bolt(bytedance/bolt)是纯源码分发的 C++ 原生引擎:官方不发布任何二进制/release/tag,
# 也没有 Docker 镜像与发行版仓库包,且 build 模式无嵌套 docker。故层 1、层 2 均无可用渠道,
# 只能走层 3(Conan + CMake 重构建)。上游无 tag,版本基线为 git ref / commit
# (请求值 = 分支名或 commit sha,匹配语义见 common.sh 的 bolt_version_accepts),
# 并要求构建产物目录存在。

# 版本校验:源码树实际版本匹配请求值,且构建产物目录存在。
bolt_version_matches() {
    bolt_ref_matches_request && [[ -d "${BOLT_BUILD_DIR}" ]]
}

# 层 1:上游官方二进制。Bolt 官方仅源码分发,不提供任何预编译二进制;本层不可用。
install_bolt_upstream_binary() {
    log "build layer 1/3: bytedance/bolt publishes no prebuilt binary (source-only); layer unavailable"
    return 1
}

# 层 2:三方仓库打包的二进制/包/镜像。Bolt 无任何版本匹配的三方包或镜像;本层不可用。
install_bolt_repo_package() {
    log "build layer 2/3: no version-matched Bolt package or image exists; layer unavailable"
    return 1
}

# 层 3:源码编译(唯一可行层)。克隆上游源码并用 Conan+CMake 构建。
install_bolt_from_source() {
    clone_bolt_source || return $?
    build_bolt || return $?
}

# build 模式分层安装:任一层通过 git ref 版本校验即停止。
install_bolt_build() {
    if install_bolt_upstream_binary && bolt_version_matches; then
        log "build layer 1/3 satisfied Bolt ${SOFTWARE_VERSION} (upstream binary)"
        return 0
    fi
    log "build layer 1/3 did not provide Bolt ${SOFTWARE_VERSION}; degrading"
    if install_bolt_repo_package && bolt_version_matches; then
        log "build layer 2/3 satisfied Bolt ${SOFTWARE_VERSION} (packaged binary)"
        return 0
    fi
    log "build layer 2/3 did not provide Bolt ${SOFTWARE_VERSION}; degrading"
    if install_bolt_from_source && bolt_version_matches; then
        log "build layer 3/3 satisfied Bolt ${SOFTWARE_VERSION} (source build)"
        return 0
    fi
    log "ERROR: no build layer provided Bolt ${SOFTWARE_VERSION}"
    return 40
}

# prebuilt:镜像未随带测试树时,在其自带源码树上补建上游测试
# (上游官方 release_with_test 目标 = release 全部选项 + skip_test=False + enable_testutil=True)。
# 被测代码与工具链均来自镜像,依赖命中镜像内 conan 缓存;但 conan options 变化会
# 触发 bolt 本体重编 + 测试二进制编译,每次运行重复付出,这是兜底方案的已知代价
# (镜像方改用 release_with_test 构建即可免除)。
ensure_prebuilt_test_tree() {
    local tree count
    if tree="$(find_ctest_tree 2>/dev/null || true)" && [[ -n "${tree}" ]] \
        && command -v ctest >/dev/null 2>&1; then
        # 树存在不等于有测试:镜像用普通 make release 构建时 CTestTestfile.cmake
        # 存在但注册 0 个测试,必须按过滤后的测试数判定是否需要补建。
        count="$(ctest --test-dir "${tree}" -N -R "${BOLT_UPSTREAM_TEST_FILTER}" 2>/dev/null |
            sed -n 's/^Total Tests: //p' | tail -n 1 || true)"
        if [[ -n "${count}" && "${count}" != "0" ]]; then
            log "image test tree present: ${tree} (${count} tests for '${BOLT_UPSTREAM_TEST_FILTER}'); no bootstrap build needed"
            return 0
        fi
        log "image test tree at ${tree} registers no tests for '${BOLT_UPSTREAM_TEST_FILTER}' (image was built with plain 'make release'); bootstrapping"
    elif [[ -n "$(find_ctest_tree 2>/dev/null || true)" ]]; then
        log "image test tree present but ctest is unavailable; cannot verify test count"
        return 0
    fi
    local -a missing=()
    command -v make >/dev/null 2>&1 || missing+=(make)
    command -v conan >/dev/null 2>&1 || missing+=(conan)
    command -v git >/dev/null 2>&1 || missing+=(git)
    command -v c++ >/dev/null 2>&1 || missing+=(c++)
    if [[ ${#missing[@]} -gt 0 ]]; then
        log "WARNING: image has no test tree and lacks toolchain (${missing[*]}); upstream-unit-test will be reported as skipped"
        return 0
    fi
    log "image ships no test tree; building upstream tests from the image's own source tree at ${BOLT_HOME} (upstream release_with_test target; deps hit the image's conan cache)"
    (cd "${BOLT_HOME}" && make release_with_test BUILD_VERSION="${SOFTWARE_VERSION}") || {
        log "ERROR: in-image bootstrap build of the upstream test tree failed"
        return 40
    }
    log "in-image test tree build finished"
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?

    local actual
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:定位构建产物并校验版本;测试树缺失时在其源码树上补建(见下)。
        BOLT_HOME="$(resolve_bolt_root || true)"
        [[ -n "${BOLT_HOME}" && -d "${BOLT_HOME}" ]] || {
            log "ERROR: prebuilt Bolt is not present in the software image (checked /workspace/bolt, /opt/bolt, /usr/local/bolt, /usr/lib/bolt, ~/bolt, BUILD_VERSION markers and bolt source trees under common prefixes; image must ship the built Bolt tree)"
            return 40
        }
        require_test_commands || return $?
        # 版本证据两级:BUILD_VERSION 文件 → 源码树 git HEAD(镜像常为完整
        # clone+checkout 检出,短 sha 即版本基线,与请求值前缀互认)。
        if [[ -f "${BOLT_HOME}/BUILD_VERSION" ]]; then
            actual="$(tr -d '\r\n' <"${BOLT_HOME}/BUILD_VERSION")"
            log "prebuilt version from BUILD_VERSION file: ${actual}"
        elif ref="$(bolt_effective_ref "${BOLT_HOME}" 2>/dev/null)" && [[ -n "${ref}" ]]; then
            actual="${ref}"
            log "prebuilt version from source-tree git HEAD: ${actual} (commit $(bolt_full_commit "${BOLT_HOME}" 2>/dev/null || true))"
        else
            log "ERROR: cannot determine the prebuilt Bolt version (no BUILD_VERSION file and no git source tree under ${BOLT_HOME})"
            return 40
        fi
    else
        require_build_commands || return $?
        ensure_conan || return $?
        install_bolt_build || return $?
        # 上游无 tag:actual = 分支名或短 commit;完整 commit sha 记入日志便于追溯。
        actual="$(bolt_effective_ref || true)"
        log "Bolt ${actual} built at commit $(bolt_full_commit 2>/dev/null || true)"
    fi

    # prebuilt(actual = 镜像内 BUILD_VERSION 或源码树 git HEAD)与
    # build(actual = ref/短 sha)统一用共享匹配语义:精确相等,或短 sha 与完整 sha 互为前缀。
    bolt_version_accepts "${actual}" || {
        log "ERROR: Bolt ${actual:-unknown}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        ensure_prebuilt_test_tree || return $?
    fi
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "Bolt ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare Bolt for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    Bolt version / git ref (default: ${SOFTWARE_VERSION})
  --results-dir DIR    Persistent result directory
  --os OS_LABEL        OS label (default: detect from /etc/os-release)
  --hardware HW_LABEL  Hardware label, e.g. kunpeng920
  --keep-workdir       Keep the isolated work directory
  -h, --help           Show this help
USAGE
}

run_build_standalone() {
    local status=0
    initialize_runtime || return $?
    func_prepare || return $?
    func_install || status=$?
    return "${status}"
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
    configure_runtime_paths || return $?
    mkdir -p "${RESULTS_DIR}" || return $?
    run_build_standalone
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
    main "$@"
fi