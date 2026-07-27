#!/usr/bin/env bash
set -euo pipefail

# ── buildctl-build.sh
# Translates docker/build-push-action inputs into buildctl build command.
# Called by composite action .github/actions/build-push-action/action.yml

echo "::group::buildctl: parsing inputs"

# ── required arguments ──
CONTEXT="${INPUT_CONTEXT:-.}"
FILE="${INPUT_FILE:-Dockerfile}"
PUSH="${INPUT_PUSH:-false}"
BUILD_ARGS="${INPUT_BUILD_ARGS:-}"
TAGS="${INPUT_TAGS:-}"
LABELS="${INPUT_LABELS:-}"
CACHE_FROM="${INPUT_CACHE_FROM:-}"
CACHE_TO="${INPUT_CACHE_TO:-}"
SECRETS="${INPUT_SECRETS:-}"
OUTPUTS="${INPUT_OUTPUTS:-}"
NO_CACHE="${INPUT_NO_CACHE:-false}"
PULL="${INPUT_PULL:-false}"
TARGET="${INPUT_TARGET:-}"
NETWORK="${INPUT_NETWORK:-}"
SSH="${INPUT_SSH:-}"
PROVENANCE="${INPUT_PROVENANCE:-false}"
SBOM="${INPUT_SBOM:-false}"

# ── environment ──
BUILDKITD_ADDR="${BUILDKITD_ADDR:-}"
DOCKER_CONFIG="${DOCKER_CONFIG:-/home/user/.docker}"

if [ -z "${BUILDKITD_ADDR}" ]; then
  echo "::error::BUILDKITD_ADDR not set. Run on a self-hosted buildkit runner."
  exit 1
fi

# ── base buildctl command ──
CMD=(buildctl)
CMD+=("--addr=${BUILDKITD_ADDR}")

# TLS certs (may not always exist)
for cert in ca.pem cert.pem key.pem; do
  if [ -f "${DOCKER_CONFIG}/${cert}" ]; then
    case "${cert}" in
      ca.pem)   CMD+=("--tlscacert=${DOCKER_CONFIG}/${cert}") ;;
      cert.pem) CMD+=("--tlscert=${DOCKER_CONFIG}/${cert}") ;;
      key.pem)  CMD+=("--tlskey=${DOCKER_CONFIG}/${cert}") ;;
    esac
  fi
done

CMD+=(build --progress=plain --frontend dockerfile.v0)

# ── Dockerfile path ──
FILE_DIR="$(dirname "${FILE}")"
FILE_NAME="$(basename "${FILE}")"
CMD+=("--local" "context=${CONTEXT}" "--local" "dockerfile=${FILE_DIR}")
CMD+=("--opt" "filename=${FILE_NAME}")

echo "  context=${CONTEXT}  file=${FILE}"

# ── build-args ──
if [ -n "${BUILD_ARGS}" ]; then
  while IFS= read -r line || [ -n "${line}" ]; do
    line="$(echo "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "${line}" ] && continue
    CMD+=("--opt" "build-arg:${line}")
    echo "  build-arg: ${line}"
  done <<< "${BUILD_ARGS}"
fi

# ── cache-from ──
if [ -n "${CACHE_FROM}" ]; then
  while IFS= read -r line || [ -n "${line}" ]; do
    line="$(echo "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "${line}" ] && continue
    CMD+=("--import-cache" "${line}")
    echo "  cache-from: ${line}"
  done <<< "${CACHE_FROM}"
fi

# ── secrets ──
if [ -f "${DOCKER_CONFIG}/config.json" ]; then
  CMD+=("--secret" "id=dockerconfig,src=${DOCKER_CONFIG}/config.json")
fi
if [ -n "${SECRETS}" ]; then
  while IFS= read -r line || [ -n "${line}" ]; do
    line="$(echo "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "${line}" ] && continue
    IFS='=' read -r key val <<< "${line}"
    key="$(echo "${key}" | xargs)"
    # env:<key> reads from environment variable, which GitHub secrets set
    CMD+=("--secret" "id=${key},env=${key}")
    echo "  secret: ${key}"
  done <<< "${SECRETS}"
fi

# ── no-cache ──
if [ "${NO_CACHE}" = "true" ]; then
  CMD+=("--no-cache")
  echo "  no-cache: true"
fi

# ── pull ── (experimental: use --opt image-resolve-mode=prefer-remote ?)
# Not directly supported by dockerfile.v0; skip for now

# ── target ──
if [ -n "${TARGET}" ]; then
  CMD+=("--opt" "target=${TARGET}")
  echo "  target: ${TARGET}"
fi

# ── network ──
if [ -n "${NETWORK}" ]; then
  CMD+=("--opt" "network:${NETWORK}")
  echo "  network: ${NETWORK}"
fi

# ── ssh ──
if [ -n "${SSH}" ]; then
  CMD+=("--ssh" "${SSH}")
  echo "  ssh: ${SSH}"
fi

# ── labels ──
if [ -n "${LABELS}" ]; then
  while IFS= read -r line || [ -n "${line}" ]; do
    line="$(echo "${line}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
    [ -z "${line}" ] && continue
    CMD+=("--opt" "label:${line}")
  done <<< "${LABELS}"
fi

# ── provenance ──
if [ "${PROVENANCE}" = "true" ]; then
  CMD+=("--opt" "attest:type=provenance,mode=max")
  echo "  provenance: true"
fi

# ── sbom ──
if [ "${SBOM}" = "true" ]; then
  CMD+=("--opt" "attest:type=sbom,generator=sbom")
  echo "  sbom: true"
fi

# ── output (image) ──
# Tags: parse multi-line, produce comma-separated name=tag1,name=tag2
# Default cache tag: append :buildcache to the first tag's image part
OUTPUT_IMAGES=""
HAS_PUSH="${PUSH}"

if [ -n "${OUTPUTS}" ]; then
  # Custom outputs: pass through as-is (e.g. type=oci,dest=...)
  CMD+=("--output" "${OUTPUTS}")
  echo "  output: ${OUTPUTS}"
else
  if [ -n "${TAGS}" ]; then
    SEP=""
    while IFS= read -r tag || [ -n "${tag}" ]; do
      tag="$(echo "${tag}" | sed 's/^[[:space:]]*//;s/[[:space:]]*$//')"
      [ -z "${tag}" ] && continue
      OUTPUT_IMAGES="${OUTPUT_IMAGES}${SEP}name=${tag}"
      SEP=","
    done <<< "${TAGS}"
  fi

  if [ -z "${OUTPUT_IMAGES}" ]; then
    echo "::warning::build-push-action: no tags set, building locally only"
  fi

  if [ "${HAS_PUSH}" = "true" ] && [ -n "${OUTPUT_IMAGES}" ]; then
    OUTPUT="${OUTPUT_IMAGES},push=true"
  else
    OUTPUT="${OUTPUT_IMAGES}"
    OUTPUT="${OUTPUT%,}"  # remove trailing comma
  fi

  if [ -n "${OUTPUT}" ]; then
    CMD+=("--output" "type=image,${OUTPUT}")
    echo "  output: type=image,${OUTPUT}"
  else
    # Build-only mode (no push, no tags): output to local tar to verify
    CMD+=("--output" "type=dummy")
    echo "  output: type=dummy (build-only)"
  fi
fi

# ── export-cache ──
if [ -n "${CACHE_TO}" ]; then
  # docker/build-push-action cache-to format: type=registry,ref=...,mode=max
  # We use inline for single-stage Dockerfiles.
  # Detect if user explicitly set mode=max; if so, warn but still use inline.
  if echo "${CACHE_TO}" | grep -q 'mode=max'; then
    echo "::notice::build-push-action: cache-to mode=max requested, but buildkitd uses inline cache (single-stage Dockerfile)."
  fi
fi
# Always use inline cache — simplest, no separate push
CMD+=("--export-cache" "type=inline")

echo "::endgroup::"

echo "::group::buildctl: command"
printf "  %s\n" "${CMD[@]}"
echo "::endgroup::"

echo "::group::buildctl: starting build"
"${CMD[@]}"
EXIT_CODE=$?
echo "::endgroup::"

exit "${EXIT_CODE}"
