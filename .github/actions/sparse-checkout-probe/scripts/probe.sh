#!/usr/bin/env bash
set -euo pipefail

printf 'Nested helper entered; action_path=%s; pwd=%s\n' "$PROBE_ACTION_PATH" "$PWD"
test -f "$PROBE_ACTION_PATH/action.yaml"
checkout_root="$(cd "$PROBE_ACTION_PATH/../../.." && pwd)"
checkout_sha="$(git -c "safe.directory=$checkout_root" -C "$checkout_root" rev-parse HEAD)"
if [ "$checkout_sha" != "$PROBE_EXPECTED_SHA" ]; then
  printf 'Wrong action revision: expected %s, got %s\n' "$PROBE_EXPECTED_SHA" "$checkout_sha" >&2
  exit 1
fi
{
  echo 'marker=pr-only-sparse-checkout-probe-v1'
  echo "checkout-sha=$checkout_sha"
} >> "$GITHUB_OUTPUT"
