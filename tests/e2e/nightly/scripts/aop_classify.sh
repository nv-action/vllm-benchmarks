#!/bin/bash
# ============================================================
# aop_classify.sh - Check if failure is environmental
#
# Reads rules-env.txt (one regex per line).
# Writes failure_type to $GITHUB_OUTPUT:
#   env_failure     - matched env patterns
#   not_env_failure - no env patterns matched
# ============================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RULES="$SCRIPT_DIR/rules-env.txt"
LOG_DIR="/tmp/test-logs"

check_log() {
  local log_file="$1"
  local label="$2"

  if [ ! -f "$log_file" ] || [ ! -s "$log_file" ]; then
    echo "  [$label] log empty/missing -> skipped"
    return 0
  fi

  local count
  count=$(grep -ciEf "$RULES" "$log_file" 2>/dev/null || echo 0)

  echo "  [$label] env patterns matched: ${count}"

  if [ "$count" -gt 0 ]; then
    echo "  [$label] --- matches ---"
    grep -niEf "$RULES" "$log_file" | head -10
    return 1   # found env patterns
  fi
  return 0     # clean
}

echo "=== Failure Classification ==="

ENV_FOUND=0
check_log "${LOG_DIR}/pytest-driven.log" "pytest-driven" || ENV_FOUND=1
check_log "${LOG_DIR}/yaml-test.log"   "yaml-test"   || ENV_FOUND=1

if [ "$ENV_FOUND" -eq 1 ]; then
  echo "=== Result: env_failure ==="
  echo "failure_type=env_failure" >> "$GITHUB_OUTPUT"
else
  echo "=== Result: not_env_failure ==="
  echo "failure_type=not_env_failure" >> "$GITHUB_OUTPUT"
fi
