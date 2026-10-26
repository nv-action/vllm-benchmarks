#!/usr/bin/env bash
# Bolt 构建脚本:环境采集 + 准备被测软件(自源码用 Conan+CMake 构建,或校验镜像内已有构建)
#   + 准备 engine-smoke 用例(注入最小消费者,链接树内引擎目标并编译,供 check 阶段真实运行)。
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
    python3 -m pip install --upgrade --user conan ||
        python3 -m pip install --upgrade --break-system-packages conan || {
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
    # GCC 13+ 重组了标准库头(ubuntu 26.04 为 GCC 15),老配方源码里靠传递包含的
    # <cstdint> 断链(实测 thrift/0.17.0 Mutex.h: 'int64_t' has not been declared)。
    # 发行版通用解法:对所有 C++ 编译强制 -include cstdint。cstdint 是 C++11 起的
    # 标准头,对所有 C++ 包安全;仅作用于 host 上下文 CXXFLAGS,C 源码与 build
    # 上下文工具不受影响。必须放在 libc-settings 脚本之后:该脚本会整体重写 profile。
    # 上游 bolt.profile 无 [conf],不会覆盖此处的追加值。
    local conf_flag='tools.build:cxxflags+=["-include cstdint"]'
    if ! grep -qF 'tools.build:cxxflags' "${profile}"; then
        if grep -q '^\[conf\]' "${profile}"; then
            sed -i "s|^\[conf\]|[conf]\n${conf_flag}|" "${profile}"
        else
            printf '\n[conf]\n%s\n' "${conf_flag}" >>"${profile}"
        fi
        log "profile patched: force-include cstdint for GCC 13+ legacy recipes"
    fi
}

# 配置依赖配方远端(对齐上游 scripts/install-bolt-deps.sh,build 模式必做)。
# bolt 的依赖配方不在 conancenter:folly/2022.10.31.00、llvm-core/*-bolt、
# paimon-cpp/*-bolt、sonic-cpp/*-bolt 等自定义版本仅存在于 bytedance 的
# conan-center-index fork。上游做法是把该 fork 浅克隆到 conan home,注册为
# local-recipes-index 类型的本地远端(index 0,优先于 conancenter);
# 二进制仍由 --build=missing 从源码编译。
# 与上游的差异:不执行其内置的 pip install conan==2.32.0——ubuntu 26.04 的
# PEP668 管控环境下裸 pip 会失败,conan 由 ensure_conan 以 --user/--break-system-packages
# 方式安装(版本 >= 2.0.14 即支持 local-recipes-index)。
prepare_bolt_dependencies() {
    local cci_home cci_ref="${BOLT_CCI_REF:-main}"
    cci_home="${CONAN_HOME:-${HOME}/.conan2}/conan-center-index"
    log "cloning bytedance conan-center-index (ref ${cci_ref}) to register the local recipe remote"
    rm -rf -- "${cci_home}"
    GIT_TERMINAL_PROMPT=0 git clone --quiet --depth 1 --branch "${cci_ref}" \
        "${BOLT_CCI_URL:-https://github.com/bytedance/conan-center-index.git}" "${cci_home}" || {
        log "ERROR: failed to clone bytedance/conan-center-index (ref ${cci_ref})"
        return 30
    }
    conan remote remove bolt-cci-local >/dev/null 2>&1 || true
    conan remote add --index 0 --type local-recipes-index bolt-cci-local "${cci_home}" || {
        log "ERROR: failed to register the local recipe remote bolt-cci-local"
        return 30
    }
    log "conan remote bolt-cci-local ready (local-recipes-index: ${cci_home})"
}

# CMake 版本兼容(build 模式):ubuntu 26.04 系统自带 CMake 4.x,而依赖中的
# 老配方(实测 ryu/2.0.1)的 CMakeLists 只声明 cmake_minimum_required(<3.5),
# CMake >= 4 已移除该兼容性,配置直接报错。上游 CI 镜像提供 cmake 3.x
# (profile 亦 replace_tool_requires 到 cmake/3.31.10,但该替换只作用于声明了
# cmake tool_requires 的配方,老配方回退到 PATH 里的系统 cmake)。
# 处理:系统 cmake >= 4 时,pip 安装 cmake<4 到用户目录并前置 PATH,保证
# 老配方拿到 3.x;另导出 CMAKE_POLICY_VERSION_MINIMUM=3.5 兜底(CMake 4 读取、
# CMake 3 忽略),防同类旧配方漏网。
ensure_cmake_compatible() {
    command -v cmake >/dev/null 2>&1 || {
        log "ERROR: cmake is unavailable"
        return 30
    }
    local major resolved
    major="$(cmake --version 2>/dev/null | head -n 1 | sed -n 's/^cmake version \([0-9][0-9]*\)\..*/\1/p' || true)"
    if [[ -n "${major}" && "${major}" -ge 4 ]]; then
        log "system cmake ${major}.x breaks legacy third-party recipes (cmake_minimum_required < 3.5); installing cmake<4 via pip"
        python3 -m pip install --user "cmake<4" ||
            python3 -m pip install --user --break-system-packages "cmake<4" || {
            log "ERROR: failed to pip-install cmake<4"
            return 30
        }
        export PATH="${HOME}/.local/bin:${PATH}"
        hash -r
        resolved="$(cmake --version 2>/dev/null | head -n 1 || true)"
        [[ "${resolved}" =~ version\ 3\. ]] || {
            log "ERROR: cmake<4 did not take precedence in PATH ($(command -v cmake))"
            return 30
        }
        log "cmake for legacy recipes: ${resolved} ($(command -v cmake))"
    fi
    export CMAKE_POLICY_VERSION_MINIMUM=3.5
}

build_bolt() {
    local -a make_args=(BUILD_VERSION="${SOFTWARE_VERSION}")
    configure_conan_profile || return $?
    prepare_bolt_dependencies || return $?
    # 直接调用上游 conan_build 目标而非 release:release 的 recipe 会写死
    # CONAN_OPTIONS="$(CONAN_PRESTO_OPTIONS)",覆盖命令行传入值(上游 Makefile
    # 注释明确要求传选项时用 conan_build)。选项与 release 目标保持一致,并追加
    # enable_jit=False:JIT 依赖 llvm-core/19.1.7-bolt 无任何公开二进制包,源码
    # 编译 2.5h+(实测 8 核 runner 上必超 360 分钟 install 预算)。
    # enable_paimon=False:再砍 paimon-cpp 及其 avro 依赖链(中等体量),给 360
    # 分钟预算留余量。JIT/Paimon 均为上游一等公民可选项,smoke/链接用例不涉及;
    # 交付形态验收由 openEuler 镜像 cell 承担。
    make_args+=(CONAN_OPTIONS="${BOLT_CONAN_OPTIONS:--o bolt/*:spark_compatible=False -o bolt/*:enable_jit=False -o bolt/*:enable_paimon=False}")
    # 上游官方 make release 同款构建(BOLT_BUILD_TESTING 默认 OFF,不编译测试)。
    # 测试职责统一收敛到 engine-smoke(两个模式共用同一用例,见 prepare_engine_smoke):
    # gtest 单测需要额外从源码编译 gtest/duckdb/cpr 等重依赖,openEuler 镜像的
    # conan 缓存不含这些包,现场补建代价 1-2 小时以上,故整个矩阵不再跑单测。
    # 首次构建会用 Conan 从源码编译依赖,耗时较长(见 timeout_minutes)。
    (cd "${BOLT_SOURCE_DIR}" && make conan_build BUILD_TYPE=Release "${make_args[@]}") || {
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

# ---------- engine-smoke:两个模式共用的核心用例 ----------
# 用例语义(ubuntu build 与 openEuler prebuilt 完全一致):
#   在被测构建树上注入一个最小 CMake 消费者,链接树内 bolt 引擎目标,
#   编译并真实运行,证明交付的引擎库可被外部代码链接、可执行。
#   prebuilt 用镜像内构建树(/workspace/bolt/_build/Release),build 用本地构建树。
# 关键实现约束:
#   * 直接用 cmake 复用已配置的构建树(CMakeCache 保留 → 引擎对象零重编,
#     只编 1 个 smoke 编译单元 + 1 次链接,分钟级)。绝不能走上游 make——其
#     _conan_prepare 在 conan.options 与镜像构建时不一致时会删 CMakeCache.txt
#     触发全量重编(实测这是补建方案耗时 1-2 小时以上的根因)。
#   * smoke 源码目录放在被测源码树之外,结束后用 git 还原根 CMakeLists.txt,
#     被测源码树保持干净。
#   * 各阶段硬超时(重配置 10 min / 构建 20 min),最坏 ~30 分钟必出结果。

BOLT_SMOKE_TARGET="bolt_func_smoke"

# 定位已配置的 CMake 构建树(以 CMakeCache.txt 为准)。
# 本函数经命令替换调用,不得向 stdout 输出日志。
find_cmake_tree() {
    if [[ -n "${BOLT_BUILD_DIR:-}" && -f "${BOLT_BUILD_DIR}/CMakeCache.txt" ]]; then
        printf '%s\n' "${BOLT_BUILD_DIR}"
        return 0
    fi
    [[ -n "${BOLT_HOME:-}" && -d "${BOLT_HOME}" ]] || return 1
    local found
    found="$(find "${BOLT_HOME}" -maxdepth 4 -type f -name CMakeCache.txt 2>/dev/null | head -n 1 || true)"
    [[ -n "${found}" ]] || return 1
    printf '%s\n' "$(dirname "${found}")"
}

# 定位引擎库归档(符号锚点来源,也是 link-smoke 的检查对象)。
find_engine_lib_for_smoke() {
    local tree="$1" lib
    lib="$(find "${tree}" -maxdepth 3 -type f \( -name 'libbolt_engine.a' -o -name 'libbolt_engine.so*' \) 2>/dev/null | head -n 1 || true)"
    [[ -n "${lib}" ]] || return 1
    printf '%s\n' "${lib}"
}

# 从引擎库归档挑选一个 bolt 命名空间下已定义的 T 符号,作为 smoke 程序的
# 链接锚点:smoke 仅取其地址、不调用,迫使链接器从归档拉入真实引擎成员,
# 防止空 main 被整档裁剪(不引用任何符号时静态库成员不会被链接进来)。
pick_smoke_symbol() {
    local lib="$1" sym
    sym="$(nm --defined-only "${lib}" 2>/dev/null |
        awk '$2 == "T" && $3 ~ /^_ZN4bolt/ {print $3; exit}' || true)"
    if [[ -z "${sym}" ]]; then
        sym="$(nm --defined-only "${lib}" 2>/dev/null |
            awk '$2 == "T" && $3 ~ /^_Z/ {print $3; exit}' || true)"
    fi
    [[ -n "${sym}" ]] || return 1
    printf '%s\n' "${sym}"
}

# 还原被测源码树的根 CMakeLists.txt(优先 git checkout,退回备份拷贝)。
restore_smoke_cmakelists() {
    local src="$1" backup="$2"
    if [[ -d "${src}/.git" ]] && git -C "${src}" checkout -- CMakeLists.txt 2>/dev/null; then
        return 0
    fi
    cp "${backup}" "${src}/CMakeLists.txt"
}

prepare_engine_smoke() {
    local src tree lib sym smoke_dir bin
    src="${BOLT_SOURCE_DIR:-${BOLT_HOME:-}}"
    [[ -n "${src}" && -f "${src}/CMakeLists.txt" ]] || {
        log "ERROR: engine smoke requires a bolt source tree with CMakeLists.txt (looked at: ${src:-<unset>})"
        return 40
    }
    tree="$(find_cmake_tree || true)"
    [[ -n "${tree}" ]] || {
        log "ERROR: no configured CMake build tree (CMakeCache.txt) found for the engine smoke"
        return 40
    }
    command -v cmake >/dev/null 2>&1 || {
        log "ERROR: cmake is unavailable; cannot prepare the engine smoke"
        return 40
    }
    command -v nm >/dev/null 2>&1 || {
        log "ERROR: nm(binutils) is unavailable; cannot pick the smoke symbol anchor"
        return 40
    }
    lib="$(find_engine_lib_for_smoke "${tree}" || true)"
    [[ -n "${lib}" ]] || {
        log "ERROR: no libbolt_engine archive found under ${tree}"
        return 40
    }
    sym="$(pick_smoke_symbol "${lib}" || true)"
    [[ -n "${sym}" ]] || {
        log "ERROR: no defined symbol found in ${lib} for the smoke link anchor"
        return 40
    }
    log "engine smoke: anchor symbol ${sym} from ${lib}"

    smoke_dir="${PERF_WORK_DIR}/engine-smoke"
    rm -rf -- "${smoke_dir}"
    mkdir -p "${smoke_dir}"
    cat >"${smoke_dir}/main.cpp" <<EOF
// Auto-generated minimal consumer for the Bolt engine functional smoke.
// Links the in-tree bolt engine target and takes the address of one
// archive-defined symbol (${sym}) so the linker must resolve real
// engine code; never called, so no ABI assumptions are needed.
extern "C" void bolt_func_smoke_symbol() __asm__("${sym}");

int main() {
    volatile void *probe = reinterpret_cast<void *>(&bolt_func_smoke_symbol);
    return probe == nullptr ? 1 : 0;
}
EOF
    # 树内引擎目标的链接图(全部 conan 依赖)由 CMake 自行解析,不手工拼链接行。
    cat >"${smoke_dir}/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
set(BOLT_SMOKE_LIB "")
foreach(candidate bolt_engine bolt bolt::bolt_engine bolt::bolt)
    if(TARGET ${candidate})
        set(BOLT_SMOKE_LIB ${candidate})
        break()
    endif()
endforeach()
if("${BOLT_SMOKE_LIB}" STREQUAL "")
    message(FATAL_ERROR "engine smoke: no bolt engine CMake target found (tried bolt_engine, bolt, bolt::bolt_engine, bolt::bolt)")
endif()
add_executable(bolt_func_smoke main.cpp)
target_link_libraries(bolt_func_smoke PRIVATE ${BOLT_SMOKE_LIB})
EOF
    cp "${src}/CMakeLists.txt" "${smoke_dir}/CMakeLists.txt.orig"
    printf '\nadd_subdirectory("%s" bolt_func_smoke_dir)\n' "${smoke_dir}" >>"${src}/CMakeLists.txt"

    log "engine smoke: reusing configured build tree at ${tree} (engine objects are not rebuilt)"
    if ! timeout 600 cmake -S "${src}" -B "${tree}" >>"${RESULTS_DIR}/smoke_build.log" 2>&1; then
        log "ERROR: engine smoke reconfigure failed or exceeded 10 min; see smoke_build.log"
        restore_smoke_cmakelists "${src}" "${smoke_dir}/CMakeLists.txt.orig"
        return 40
    fi
    if ! timeout 1200 cmake --build "${tree}" --target "${BOLT_SMOKE_TARGET}" >>"${RESULTS_DIR}/smoke_build.log" 2>&1; then
        log "ERROR: engine smoke build failed or exceeded 20 min; see smoke_build.log"
        restore_smoke_cmakelists "${src}" "${smoke_dir}/CMakeLists.txt.orig"
        return 40
    fi
    restore_smoke_cmakelists "${src}" "${smoke_dir}/CMakeLists.txt.orig"

    bin="${tree}/bolt_func_smoke_dir/${BOLT_SMOKE_TARGET}"
    if [[ ! -x "${bin}" ]]; then
        bin="$(find "${tree}/bolt_func_smoke_dir" -maxdepth 1 -type f -name "${BOLT_SMOKE_TARGET}" 2>/dev/null | head -n 1 || true)"
    fi
    if [[ -z "${bin}" || ! -x "${bin}" ]]; then
        log "ERROR: engine smoke binary was not produced under ${tree}/bolt_func_smoke_dir"
        return 40
    fi
    mkdir -p "${RESULTS_DIR}"
    printf '%s\n' "${bin}" >"${RESULTS_DIR}/smoke_binary.path"
    log "engine smoke binary ready: ${bin}"
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
        ensure_cmake_compatible || return $?
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
    # 两个模式统一准备 engine-smoke 用例(prebuilt 用镜像构建树,build 用本地构建树)。
    prepare_engine_smoke || return $?
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