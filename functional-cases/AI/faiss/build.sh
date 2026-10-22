#!/usr/bin/env bash
# faiss 构建脚本:环境采集 + 准备被测软件(安装或校验)。
#   FUNC_MODE=prebuilt - 镜像内已装好 faiss,只定位并校验版本,不安装
#   FUNC_MODE=build    - 装依赖 + 用 uv 固定版本 CPython 建任务私有 venv
#                        + 分层优先获取(上游官方二进制→三方仓库→源码编译)
# 暴露给 Framework 的阶段:prepare、install。独立执行时依次运行这两个阶段。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=common.sh
source "${SCRIPT_DIR}/common.sh"

# ---------- 阶段:prepare ----------

func_prepare() {
    initialize_runtime || return $?
    local arch platform kernel cpu_count python_version gcc_version glibc_version
    arch="$(uname -m)"
    platform="$(sed -n 's/^PRETTY_NAME=//p' /etc/os-release 2>/dev/null | head -n 1 | tr -d '"' || true)"
    kernel="$(uname -r)"
    cpu_count="$(nproc 2>/dev/null || printf '0')"
    # 缺失的命令只记录为 unknown,不能因为探测失败而让 set -e 退出(否则静默 127)。
    python_version="$(python3 --version 2>/dev/null | awk '{print $2}' || true)"
    gcc_version="$(gcc -dumpfullversion -dumpversion 2>/dev/null | head -n 1 || true)"
    glibc_version="$(getconf GNU_LIBC_VERSION 2>/dev/null | awk '{print $2}' || true)"
    cat >"${RESULTS_DIR}/system_info.json" <<EOF
{
  "architecture": "$(json_escape "${arch}")",
  "platform": "$(json_escape "${platform}")",
  "kernel": "$(json_escape "${kernel}")",
  "cpu_count": ${cpu_count:-0},
  "python_version": "$(json_escape "${python_version:-unknown}")",
  "gcc_version": "$(json_escape "${gcc_version:-unknown}")",
  "glibc_version": "$(json_escape "${glibc_version:-unknown}")"
}
EOF
    log "prepared environment (os=${FUNC_OS}, mode=${FUNC_MODE}, hardware=${FUNC_HARDWARE:-unspecified})"
}

# ---------- 阶段:install ----------

# 安装上游官方 uv(astral-sh)静态二进制到本任务私有目录,用于拉取固定版本的 CPython。
# release 资产走 objects.githubusercontent.com,国产 Runner 不可达;下载失败时从 PyPI 索引取
# uv wheel(wheel 里的 data/scripts/uv 就是同一个静态二进制),不需要 pip 参与。
install_uv() {
    local target
    [[ -x "${UV_BIN}" ]] && return 0
    case "$(normalize_arch "${EXPECTED_ARCH}")" in
        aarch64) target="aarch64-unknown-linux-gnu" ;;
        x86_64) target="x86_64-unknown-linux-gnu" ;;
        *)
            log "ERROR: unsupported architecture for uv: ${EXPECTED_ARCH}"
            return 30
            ;;
    esac
    rm -rf -- "${UV_DIR}" || return 30
    mkdir -p "${UV_DIR}" || return 30
    if ! download_uv_release "${target}"; then
        log "release download failed; falling back to the uv wheel from ${UV_PYPI_INDEX}"
        install_uv_from_index "${target}" || return 30
    fi
    [[ -x "${UV_BIN}" ]] || {
        log "ERROR: uv binary is unavailable after install: ${UV_BIN}"
        return 30
    }
    return 0
}

# 第一选择:官方 release 包(直连或 UV_RELEASE_BASE 指定的镜像)。
download_uv_release() {
    local target="$1" archive="${PERF_WORK_DIR}/uv.tar.gz"
    log "downloading official uv ${UV_VERSION} (${target}) from ${UV_RELEASE_BASE}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${archive}" \
        "${UV_RELEASE_BASE}/${UV_VERSION}/uv-${target}.tar.gz" || {
        rm -f "${archive}"
        return 1
    }
    tar -xzf "${archive}" -C "${UV_DIR}" --strip-components=1 || {
        log "ERROR: failed to extract uv"
        rm -f "${archive}"
        return 1
    }
    rm -f "${archive}"
    return 0
}

# 兜底:从 PyPI 简单索引定位对应架构的 uv wheel,直接解出静态二进制。
install_uv_from_index() {
    local target="$1" page="${PERF_WORK_DIR}/uv-index.html" wheel="${PERF_WORK_DIR}/uv.whl" url
    curl -fsSL --retry 2 --connect-timeout 30 -o "${page}" "${UV_PYPI_INDEX%/}/uv/" || {
        log "ERROR: failed to fetch the uv index from ${UV_PYPI_INDEX}"
        return 30
    }
    url="$(python3 - "${page}" "${UV_VERSION}" "${target}" "${UV_PYPI_INDEX}" <<'PY'
import re, sys, urllib.parse

page, version, target, index = sys.argv[1:5]
arch = 'aarch64' if target.startswith('aarch64') else 'x86_64'
html = open(page, encoding='utf-8', errors='replace').read()
wheels = [(name, href) for href, name in
          re.findall(r'href="([^"]+\.whl)(?:#[^"]*)?"[^>]*>([^<]+\.whl)<', html)
          if name.startswith(f'uv-{version}-py3-none-') and arch in name]
if not wheels:
    sys.exit(1)
# manylinux_2_17 兼容性最广,优先;其次 manylinux_2_28、musllinux。
for prefer in ('manylinux_2_17', 'manylinux_2_28', 'musllinux'):
    chosen = next((href for name, href in wheels if prefer in name), None)
    if chosen:
        break
else:
    chosen = wheels[0][1]
print(urllib.parse.urljoin(f"{index.rstrip('/')}/uv/", chosen))
PY
)" || {
        log "ERROR: no uv ${UV_VERSION} wheel for ${target} in ${UV_PYPI_INDEX}"
        return 30
    }
    log "downloading uv ${UV_VERSION} wheel: ${url}"
    curl -fsSL --retry 3 --connect-timeout 30 -o "${wheel}" "${url}" || {
        log "ERROR: failed to download the uv wheel"
        return 30
    }
    if ! python3 - "${wheel}" "${UV_BIN}" <<'PY'
import os, sys, zipfile

wheel, dest = sys.argv[1:3]
with zipfile.ZipFile(wheel) as archive:
    member = next((n for n in archive.namelist() if n.endswith('.data/scripts/uv')), None)
    if member is None:
        sys.exit(f'no uv binary in {wheel}')
    with archive.open(member) as source, open(dest, 'wb') as target:
        target.write(source.read())
os.chmod(dest, 0o755)
PY
    then
        log "ERROR: failed to extract the uv binary from the wheel"
        return 30
    fi
    rm -f "${page}" "${wheel}"
    return 0
}

# 用 uv 拉取固定版本 CPython 并据此创建任务私有 venv。
# faiss-cpu 的 Linux wheel 是 cpXX 专属标签(1.8.0 只到 cp312),而 ubuntu 26.04 自带 3.14
# 选不到该 wheel,venv 又无法改变解释器版本,所以必须用 uv 的独立 CPython 构建。
create_faiss_venv() {
    local actual_python
    export UV_PYTHON_INSTALL_DIR="${UV_PYTHON_DIR}"
    export UV_PYTHON_PREFERENCE=only-managed
    if [[ -e "${VENV_DIR}" ]]; then
        log "ERROR: virtual environment is not clean: ${VENV_DIR}"
        return 20
    fi
    log "installing CPython ${FAISS_PYTHON_VERSION} via uv into ${UV_PYTHON_DIR} (mirror: ${UV_PYTHON_INSTALL_MIRROR})"
    if ! "${UV_BIN}" python install "${FAISS_PYTHON_VERSION}"; then
        # 镜像缺对应构建号或临时不可达时,退回官方 python-build-standalone 源再试一次。
        log "mirror ${UV_PYTHON_INSTALL_MIRROR} did not provide CPython ${FAISS_PYTHON_VERSION}; retrying the official source"
        unset UV_PYTHON_INSTALL_MIRROR
        "${UV_BIN}" python install "${FAISS_PYTHON_VERSION}" || {
            log "ERROR: uv failed to install CPython ${FAISS_PYTHON_VERSION}"
            return 40
        }
    fi
    log "creating task-private venv from CPython ${FAISS_PYTHON_VERSION}"
    # --seed 让 venv 自带 pip/setuptools,后续各层沿用 pip 安装产物。
    "${UV_BIN}" venv --python "${FAISS_PYTHON_VERSION}" --seed "${VENV_DIR}" || {
        log "ERROR: failed to create the virtual environment"
        return 40
    }
    [[ -x "${VENV_DIR}/bin/pip" ]] || {
        log "ERROR: the virtual environment has no usable pip"
        return 40
    }
    actual_python="$("${VENV_DIR}/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    [[ "${actual_python}" == "${FAISS_PYTHON_VERSION}" ]] || {
        log "ERROR: venv python is ${actual_python}, expected ${FAISS_PYTHON_VERSION}"
        return 40
    }
    log "venv ready with CPython ${actual_python}"
}

# build 模式:先用 uv 拉固定版本 CPython 建任务私有 venv,再分层优先获取 faiss
# (见 common.sh acquire_build_layers)。
install_faiss_build() {
    install_uv || return $?
    create_faiss_venv || return $?
    acquire_build_layers \
        fetch_faiss_upstream_binary \
        fetch_faiss_third_party \
        fetch_faiss_source || return $?
}

# 层 1:上游官方二进制。
# faiss 官方不在 PyPI 发布独立 wheel/tarball,也没有官方 release 二进制资产
# (config/distribution.yaml note:官方不在 PyPI 分发,faiss-cpu 为第三方;官方唯一推荐 conda)。
# 本层无可用官方二进制,按分层设计显式跳过,交给下一层。
fetch_faiss_upstream_binary() {
    log "layer 1: faiss ships no upstream standalone binary (source + conda only); skip"
    return 1
}

# 层 2:三方仓库已打包二进制。
# 官方唯一推荐渠道是 conda(pytorch channel 的 faiss-cpu)。build 模式运行在普通容器内,
# 没有可用的 conda 运行时(被测环境统一为任务私有 venv),按"渠道不可用即改用等效产物"的规则,
# 改用等价的预编译 PyPI wheel faiss-cpu(第三方打包的官方 CPU 产物),
# 强制 --only-binary=:all: 以确保拿到二进制而非现场编译源码包。
# wheel 有 cpXX ABI 约束,venv 的 CPython 版本已由 create_faiss_venv 按 wheel 标签选定。
# 老版本 wheel 的元数据会漏声明运行期依赖(faiss 1.8.0 的 loader.py 要 import packaging,
# 元数据里没有;numpy 也没写上限,会拉到与 1.x 扩展不兼容的 numpy 2.x,上游事后以
# 1.8.0.post1 补齐了这两项),所以由这里显式补上,pip 对已声明的版本重复指定无副作用。
fetch_faiss_third_party() {
    local -a index_args=() extra=(packaging)
    local major minor
    mapfile -t index_args < <(pip_index_args)
    IFS='.' read -r major minor _ <<<"${SOFTWARE_VERSION}"
    if [[ "${major}" == "1" && "${minor}" -lt 9 ]]; then
        extra+=("numpy<2")
    fi
    log "layer 2: installing the equivalent prebuilt wheel ${FAISS_PACKAGE_SPEC} (python ${FAISS_PYTHON_VERSION}, extra: ${extra[*]})"
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" --only-binary=:all: "${FAISS_PACKAGE_SPEC}" "${extra[@]}"
}

# 层 3:源码编译(最后手段)。
# 克隆官方仓库精确 tag v${SOFTWARE_VERSION},用官方 CMake 打开 Python 绑定现场构建。
fetch_faiss_source() {
    require_faiss_source_tools || return $?
    local -a index_args=()
    mapfile -t index_args < <(pip_index_args)
    local src="${BUILD_DIR}/faiss-source" build="${BUILD_DIR}/faiss-build"
    rm -rf -- "${src}" "${build}"
    log "layer 3: cloning faiss v${SOFTWARE_VERSION} from ${FAISS_SOURCE_URL}"
    GIT_TERMINAL_PROMPT=0 git clone --branch "v${SOFTWARE_VERSION}" --depth 1 \
        "${FAISS_SOURCE_URL}" "${src}" || {
        log "ERROR: failed to clone faiss v${SOFTWARE_VERSION}"
        return 30
    }
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${index_args[@]}" numpy swig || {
        log "ERROR: failed to install faiss python build dependencies"
        return 40
    }
    # 必须显式指定解释器:未指定时 faiss 的 FindPython 会探测到系统 python3,
    # 而 numpy/pip 装在本任务的私有 venv 里,于是找不到 NumPy 头文件而 configure 失败。
    cmake -B "${build}" -S "${src}" \
        -DPython_EXECUTABLE="${VENV_DIR}/bin/python" \
        -DPython3_EXECUTABLE="${VENV_DIR}/bin/python" \
        -DFAISS_ENABLE_GPU=OFF -DFAISS_ENABLE_PYTHON=ON -DFAISS_ENABLE_EXTRAS=OFF \
        -DBUILD_TESTING=OFF -DBUILD_SHARED_LIBS=ON -DCMAKE_BUILD_TYPE=Release || {
        log "ERROR: faiss cmake configure failed"
        return 40
    }
    make -C "${build}" -j"$(nproc)" faiss swigfaiss || {
        log "ERROR: faiss build failed"
        return 40
    }
    "${VENV_DIR}/bin/pip" install --no-cache-dir --disable-pip-version-check \
        "${build}/faiss/python" || {
        log "ERROR: failed to install faiss python bindings from source"
        return 40
    }
}

func_install() {
    initialize_runtime || return $?
    check_architecture || return $?
    require_python_runtime || return $?

    if [[ "${FUNC_MODE}" == "prebuilt" ]]; then
        # 镜像已装好:只定位并校验版本。
        [[ -n "${PYBIN}" && -x "${PYBIN}" ]] || {
            log "ERROR: python3 is unavailable in the software image"
            return 40
        }
        log "using the pre-installed faiss from the software image"
    else
        install_faiss_build || return $?
    fi

    local actual
    actual="$(read_installed_version)"
    [[ -n "${actual}" ]] || {
        log "ERROR: cannot detect the installed faiss version"
        return 40
    }
    [[ "${actual}" == "${SOFTWARE_VERSION}" ]] || {
        log "ERROR: faiss ${actual}, expected ${SOFTWARE_VERSION}"
        return 40
    }
    mkdir -p "$(dirname "${PERF_ACTUAL_VERSION_FILE}")"
    printf '%s\n' "${actual}" >"${PERF_ACTUAL_VERSION_FILE}" || return 40
    log "faiss ${actual} ready (mode=${FUNC_MODE})"
}

# ---------- 独立运行入口 ----------

usage() {
    cat <<USAGE
Usage: $(basename "$0") [OPTIONS]
Capture the environment and prepare faiss for one OS/hardware cell.
  --mode prebuilt | build   (default: ${FUNC_MODE})

Options:
  --version VERSION    faiss version (default: ${SOFTWARE_VERSION})
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