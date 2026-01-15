#!/bin/bash
set -euo pipefail

# Periodically cleanup buyer submission replay/idempotency tables.
#
# Env:
# - DATABASE_URL (required)
# - SLEEP_SECONDS (optional, default: 21600 = 6 hours)
#
# This script intentionally does not print DATABASE_URL or any tokens.

SLEEP_SECONDS="${SLEEP_SECONDS:-21600}"

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: missing DATABASE_URL" >&2
  exit 2
fi

PY=""
if command -v python3 >/dev/null 2>&1; then
  PY="python3"
elif command -v python >/dev/null 2>&1; then
  PY="python"
else
  echo "ERROR: python runtime not found (python3/python)" >&2
  exit 2
fi

while true; do
  echo "cleanup_start_utc=$(date -u +%FT%TZ)"
  "$PY" scripts/cleanup_buyer_submission_tables.py --apply
  echo "cleanup_sleep_seconds=$SLEEP_SECONDS"
  sleep "$SLEEP_SECONDS"
done

