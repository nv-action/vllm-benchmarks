#!/usr/bin/env bash
# Build the pinned upstream Sysbench client in this task's private directory.

set -euo pipefail

usage() {
    cat <<'USAGE'
Usage:
  prepare_sysbench.sh WORK_DIRECTORY MYSQL_CONFIG

Builds the pinned upstream Sysbench source inside WORK_DIRECTORY.
USAGE
}

if [[ "$#" -ne 2 ]]; then
    usage >&2
    exit 10
fi

work_directory="${1%/}"
mysql_config="${2}"
sysbench_source_directory="${work_directory}/sysbench-1.0.17"
sysbench_repository="https://github.com/akopytov/sysbench.git"
sysbench_ref="1.0.17"
sysbench_commit="d634bce"

for command_name in git make gcc autoreconf libtoolize pkg-config; do
    if ! command -v "${command_name}" >/dev/null 2>&1; then
        printf '[mysql] ERROR: Sysbench prerequisite is missing: %s\n' \
            "${command_name}" >&2
        exit 30
    fi
done
if [[ ! -x "${mysql_config}" ]]; then
    printf '[mysql] ERROR: MySQL mysql_config is missing: %s\n' "${mysql_config}" >&2
    exit 30
fi
mkdir -p "${work_directory}"
rm -rf -- "${sysbench_source_directory}"
printf '[mysql] cloning official Sysbench %s from %s\n' \
    "${sysbench_ref}" "${sysbench_repository}"
git clone --quiet --depth 1 --branch "${sysbench_ref}" \
    "${sysbench_repository}" "${sysbench_source_directory}"

actual_commit="$(git -C "${sysbench_source_directory}" rev-parse --short=7 HEAD)"
if [[ "${actual_commit}" != "${sysbench_commit}" ]]; then
    printf '[mysql] ERROR: Sysbench %s resolved to %s, expected %s\n' \
        "${sysbench_ref}" "${actual_commit}" "${sysbench_commit}" >&2
    exit 30
fi

printf '[mysql] building official Sysbench %s\n' "${sysbench_ref}"
(
    cd "${sysbench_source_directory}"
    ./autogen.sh
    PATH="$(dirname "${mysql_config}"):${PATH}" ./configure --with-mysql
    make -j"$(getconf _NPROCESSORS_ONLN)"
)
if [[ ! -x "${sysbench_source_directory}/src/sysbench" ]]; then
    printf '%s\n' '[mysql] ERROR: Sysbench build did not create src/sysbench' >&2
    exit 40
fi

lua_directory="${sysbench_source_directory}/src/lua"
for workload in read_write read_only write_only; do
    script="${lua_directory}/oltp_${workload}.lua"
    if [[ ! -f "${script}" ]]; then
        printf '[mysql] ERROR: Sysbench workload script is missing: %s\n' "${script}" >&2
        exit 40
    fi
    if ! LUA_PATH="${lua_directory}/?.lua;;" \
        "${sysbench_source_directory}/src/sysbench" "${script}" help >/dev/null; then
        printf '[mysql] ERROR: Sysbench cannot load workload script: %s\n' "${script}" >&2
        exit 40
    fi
done

printf '[mysql] Sysbench %s is ready\n' "${sysbench_ref}"
