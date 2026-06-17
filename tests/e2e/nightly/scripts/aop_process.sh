#!/bin/bash
# ============================================================
# aop_process.sh - Handle a recent real failure
#
# Args: failure_type commit_age_days runner tests config
#       pytest_summary yaml_summary
# ============================================================
set -euo pipefail

FT="${1:-unknown}"
AGE="${2:-?}"
RUNNER="${3:-?}"
TESTS="${4:-}"
CONFIG="${5:-}"
PYTEST_SUMMARY="${6:-}"
YAML_SUMMARY="${7:-}"

echo "================================================"
echo " PROCESS - needs attention"
echo "   Failure type : ${FT}"
echo "   Commit age   : ${AGE} days"
echo "   Runner       : ${RUNNER}"
echo "   Tests        : ${TESTS:-N/A}"
echo "   Config       : ${CONFIG:-N/A}"
echo "   PyTest       : ${PYTEST_SUMMARY:-N/A}"
echo "   YAML         : ${YAML_SUMMARY:-N/A}"
echo "================================================"

echo "::group::Failed test details"
for f in /tmp/test-logs/pytest-driven.log /tmp/test-logs/yaml-test.log; do
  if [ -f "$f" ]; then
    grep -A 10 'FAILED' "$f" || true
  fi
done
echo "::endgroup::"

# =====================================================
# TODO: 在这里执行你的进一步处理命令
# 例如: 发 webhook、提 issue、发企业微信通知等
# =====================================================
