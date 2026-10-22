#!/usr/bin/env bash
set -euo pipefail

# Global cleanup for dedicated performance runners. Software processes inherit
# a run isolation token; cwd/exe/fd and work-root references provide a fallback.

MODE="${1:-}"
WORK_ROOT="${PERF_WORK_ROOT:-/home/runner/boostkit-perf}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

log() { printf '[cleanup] %s\n' "$*"; }
fail() { printf '[cleanup] ERROR: %s\n' "$*" >&2; exit 1; }

if [[ "${PERF_DEDICATED_RUNNER:-}" != "true" ]]; then
    fail "PERF_DEDICATED_RUNNER=true is required; refusing global cleanup"
fi
if [[ "${WORK_ROOT}" != "/home/runner/boostkit-perf" ]]; then
    fail "PERF_WORK_ROOT must be exactly /home/runner/boostkit-perf"
fi
if [[ "${MODE}" != "--before" && "${MODE}" != "--after" && "${MODE}" != "--verify" ]]; then
    fail "usage: $0 --before|--after|--verify"
fi

matching_processes() {
    python3 "${SCRIPT_DIR}/process_scanner.py" \
        --root "${WORK_ROOT}" \
        --exclude "$$" \
        --exclude "${PPID}"
}

describe_processes() {
    local pid
    while read -r pid; do
        [[ -n "${pid}" ]] || continue
        ps -p "${pid}" -o pid=,user=,args= 2>/dev/null || true
    done
}

matching_mounts() {
    command -v findmnt >/dev/null 2>&1 || return 0
    findmnt -rn -o TARGET | awk -v root="${WORK_ROOT}/" 'index($0, root) == 1'
}

verify_clean() {
    local dirty=0
    if [[ -n "$(matching_processes)" ]]; then
        log "residual isolated performance processes still exist"
        matching_processes | describe_processes >&2
        dirty=1
    fi
    if [[ -d "${WORK_ROOT}" ]] && find "${WORK_ROOT}" -mindepth 1 -print -quit | grep -q .; then
        log "${WORK_ROOT} is not empty"
        dirty=1
    fi
    if matching_mounts | grep -q .; then
        log "mounts remain below ${WORK_ROOT}"
        dirty=1
    fi
    [[ "${dirty}" -eq 0 ]] || fail "runner cleanliness verification failed; quarantine this runner"
    log "runner cleanliness verification passed"
}

if [[ "${MODE}" == "--verify" ]]; then
    verify_clean
    exit 0
fi

log "starting ${MODE#--} global cleanup on dedicated runner"

mapfile -t pids < <(matching_processes)
if ((${#pids[@]})); then
    kill -TERM "${pids[@]}" 2>/dev/null || true
    mapfile -t pids < <(matching_processes)
    if ((${#pids[@]})); then
        kill -KILL "${pids[@]}" 2>/dev/null || true
    fi
fi

mkdir -p "${WORK_ROOT}"
if command -v findmnt >/dev/null 2>&1; then
    mapfile -t mounts < <(matching_mounts | sort -r)
    for mountpoint in "${mounts[@]}"; do
        [[ "${mountpoint}" == "${WORK_ROOT}" ]] && continue
        umount --lazy --force "${mountpoint}"
    done
fi
find "${WORK_ROOT}" -mindepth 1 -delete

verify_clean
