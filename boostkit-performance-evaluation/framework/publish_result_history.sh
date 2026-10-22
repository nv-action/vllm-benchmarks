#!/usr/bin/env bash
set -euo pipefail

SOURCE_DIR="${1:?usage: publish_result_history.sh SOURCE_DIR [BRANCH]}"
BRANCH="${2:-performance-results}"

[[ "${BRANCH}" =~ ^[A-Za-z0-9._/-]+$ ]] || {
    printf '[history] invalid branch name: %s\n' "${BRANCH}" >&2
    exit 2
}
[[ -d "${SOURCE_DIR}" ]] || {
    printf '[history] source directory does not exist: %s\n' "${SOURCE_DIR}" >&2
    exit 2
}

SOURCE_DIR="$(cd "${SOURCE_DIR}" && pwd)"
REPOSITORY_ROOT="$(git rev-parse --show-toplevel)"
TEMP_PARENT="${RUNNER_TEMP:-/tmp}"
WORKTREE="$(mktemp -d "${TEMP_PARENT}/performance-results.XXXXXX")"
rmdir "${WORKTREE}"

cleanup() {
    git -C "${REPOSITORY_ROOT}" worktree remove --force "${WORKTREE}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

if git ls-remote --exit-code --heads origin "refs/heads/${BRANCH}" >/dev/null 2>&1; then
    git -C "${REPOSITORY_ROOT}" fetch origin "${BRANCH}:refs/remotes/origin/${BRANCH}"
    git -C "${REPOSITORY_ROOT}" worktree add -B "${BRANCH}" "${WORKTREE}" "refs/remotes/origin/${BRANCH}"
else
    git -C "${REPOSITORY_ROOT}" worktree add --detach "${WORKTREE}" HEAD
    git -C "${WORKTREE}" switch --orphan "${BRANCH}"
fi

MANIFEST_COUNT=0
while IFS= read -r -d '' MANIFEST; do
    MANIFEST_COUNT=$((MANIFEST_COUNT + 1))
    RELATIVE_DIRECTORY="${MANIFEST#"${SOURCE_DIR}/"}"
    RELATIVE_DIRECTORY="${RELATIVE_DIRECTORY%/manifest.json}"
    if [[ -e "${WORKTREE}/${RELATIVE_DIRECTORY}" ]]; then
        printf '[history] immutable run already exists: %s\n' "${RELATIVE_DIRECTORY}" >&2
        exit 3
    fi
done < <(find "${SOURCE_DIR}" -mindepth 5 -maxdepth 5 -type f -name manifest.json -print0)
[[ "${MANIFEST_COUNT}" -gt 0 ]] || {
    printf '[history] no run manifest found in source directory\n' >&2
    exit 2
}

cp -a "${SOURCE_DIR}/." "${WORKTREE}/"
git -C "${WORKTREE}" config user.name "github-actions[bot]"
git -C "${WORKTREE}" config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git -C "${WORKTREE}" add --all

if git -C "${WORKTREE}" diff --cached --quiet; then
    printf '[history] no permanent result changes to publish\n'
    exit 0
fi

git -C "${WORKTREE}" commit -m "Record performance run ${GITHUB_RUN_ID:-unknown}-${GITHUB_RUN_ATTEMPT:-1}"
git -C "${WORKTREE}" push origin "HEAD:refs/heads/${BRANCH}"
printf '[history] published permanent results to %s\n' "${BRANCH}"
