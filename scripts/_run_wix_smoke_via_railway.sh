#!/usr/bin/env bash
# Wrapper that bridges the local smoke script into the Railway prod container.
#
# Why this shape: `railway ssh` allocates a PTY and does NOT proxy stdin, so
# heredoc / pipe-to-bash patterns leave you in an interactive shell. The only
# reliable transport is the `bash -c "<single string>"` argv. To avoid the
# multi-layer quoting hell of embedding a Python script in that string, the
# script is base64-encoded LOCALLY and the result is inlined as a constant
# token (only A-Z a-z 0-9 + / = appear, so the outer double-quote is safe).
#
# Usage:
#   ./scripts/_run_wix_smoke_via_railway.sh               # dry-run
#   ./scripts/_run_wix_smoke_via_railway.sh --live        # real upstream POST

set -euo pipefail

MODE_FLAG=""
if [[ "${1:-}" == "--live" ]]; then
  MODE_FLAG="--live"
fi

MERCHANT_ID="${MERCHANT_ID:-merch_efbc46b4619cfbdf}"
SCRIPT_LOCAL="$(cd "$(dirname "$0")" && pwd)/smoke_wix_order_writeback.py"

if [[ ! -f "$SCRIPT_LOCAL" ]]; then
  echo "ERROR: $SCRIPT_LOCAL not found" >&2
  exit 2
fi

SCRIPT_B64=$(base64 < "$SCRIPT_LOCAL" | tr -d '\n')

# Single bash -c arg, inlined base64 (no shell-var hand-off, no stdin).
railway ssh -- bash -c "set -e; mkdir -p /app/scripts; echo $SCRIPT_B64 | base64 -d > /app/scripts/_wix_smoke.py; cd /app; python3 scripts/_wix_smoke.py --merchant-id $MERCHANT_ID $MODE_FLAG; rm -f /app/scripts/_wix_smoke.py"
