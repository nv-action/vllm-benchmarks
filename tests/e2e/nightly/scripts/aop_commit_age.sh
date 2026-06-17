#!/bin/bash
# ============================================================
# aop_commit_age.sh - Look up test config in device CSV table
#
# CSV format:
#   <config_name>,<last_status>,<last_date>
#   qwen3-32b-int8,pass,2026-06-15
#   deepseek-r1-w8a8,fail,2026-06-10
#
# Args: $1 = config_name (e.g. "qwen3-32b-int8")
#       $2 = csv_path   (default: /root/.cache/commit-table.csv)
#
# Writes to $GITHUB_OUTPUT:
#   commit_age_days  - days since last recorded date
#   is_old           - true if > 3 days
#   last_status      - pass/fail from table
#   last_date        - date from table
# ============================================================
set -euo pipefail

CONFIG_NAME="${1:-}"
CSV_PATH="${2:-/root/.cache/commit-table.csv}"

if [ -z "$CONFIG_NAME" ]; then
  echo "ERROR: no config name provided"
  exit 1
fi

echo ">>> Looking up config : ${CONFIG_NAME}"
echo ">>> CSV path          : ${CSV_PATH}"

if [ ! -f "$CSV_PATH" ]; then
  echo ">>> CSV not found → treating as recent"
  echo "is_old=false"     >> "$GITHUB_OUTPUT"
  echo "commit_age_days=0" >> "$GITHUB_OUTPUT"
  echo "last_status=unknown" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Find the row for this config
ROW=$(grep -F "$CONFIG_NAME" "$CSV_PATH" | head -1 || true)

if [ -z "$ROW" ]; then
  echo ">>> Config '${CONFIG_NAME}' not found in CSV → treating as recent"
  echo "is_old=false"     >> "$GITHUB_OUTPUT"
  echo "commit_age_days=0" >> "$GITHUB_OUTPUT"
  echo "last_status=unknown" >> "$GITHUB_OUTPUT"
  exit 0
fi

# Parse: config_name,last_status,last_date
LAST_STATUS=$(echo "$ROW" | cut -d',' -f2 | xargs)
LAST_DATE=$(echo "$ROW"   | cut -d',' -f3 | xargs)

if [ -z "$LAST_DATE" ]; then
  echo ">>> Could not parse date from row: ${ROW}"
  echo "is_old=false"     >> "$GITHUB_OUTPUT"
  echo "commit_age_days=0" >> "$GITHUB_OUTPUT"
  echo "last_status=${LAST_STATUS:-unknown}" >> "$GITHUB_OUTPUT"
  exit 0
fi

echo "last_status=${LAST_STATUS}" >> "$GITHUB_OUTPUT"
echo "last_date=${LAST_DATE}"     >> "$GITHUB_OUTPUT"

LAST_TS=$(date -d "$LAST_DATE" +%s 2>/dev/null || true)
if [ -z "$LAST_TS" ]; then
  echo ">>> Could not parse date: ${LAST_DATE}"
  echo "is_old=false"     >> "$GITHUB_OUTPUT"
  echo "commit_age_days=0" >> "$GITHUB_OUTPUT"
  exit 0
fi

NOW=$(date +%s)
AGE_DAYS=$(( (NOW - LAST_TS) / 86400 ))

echo "commit_age_days=${AGE_DAYS}" >> "$GITHUB_OUTPUT"

if [ "$AGE_DAYS" -gt 3 ]; then
  echo "is_old=true" >> "$GITHUB_OUTPUT"
  echo ">>> ${CONFIG_NAME} last_status=${LAST_STATUS} date=${LAST_DATE} age=${AGE_DAYS}d (> 3 days) → old"
else
  echo "is_old=false" >> "$GITHUB_OUTPUT"
  echo ">>> ${CONFIG_NAME} last_status=${LAST_STATUS} date=${LAST_DATE} age=${AGE_DAYS}d (<= 3 days) → recent"
fi
