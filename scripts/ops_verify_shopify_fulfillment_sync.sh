#!/usr/bin/env bash
set -euo pipefail

ORDER_ID="${1:-${ORDER_ID:-}}"
if [[ -z "${ORDER_ID}" ]]; then
  echo "Usage: $0 <ORDER_ID>" >&2
  echo "Or set env ORDER_ID=..." >&2
  exit 2
fi

BASE_URL="${BASE_URL:-https://web-production-fedb.up.railway.app}"
DATABASE_URL_FILE="${DATABASE_URL_FILE:-$HOME/Desktop/prod_DATABASE_URL.txt}"

db_args=()
if [[ -n "${DATABASE_URL:-}" ]]; then
  db_args+=(--database-url "$DATABASE_URL")
elif [[ -f "$DATABASE_URL_FILE" ]]; then
  db_args+=(--database-url-file "$DATABASE_URL_FILE")
else
  echo "ERROR: missing database url. Set DATABASE_URL or create $DATABASE_URL_FILE" >&2
  exit 2
fi

out="$(python3 scripts/ops_simulate_shopify_orders_updated_webhook.py \
  --base-url "$BASE_URL" \
  --order-id "$ORDER_ID" \
  "${db_args[@]}")"

echo "$out"

after="$(echo "$out" | awk -F': ' '/^pivota_after:/ {print $2; exit}')"
if [[ -z "$after" ]]; then
  echo "ERROR: could not parse pivota_after line" >&2
  exit 1
fi
if [[ "$after" == *"fulfillment=shipped"* ]]; then
  exit 0
fi

echo "ERROR: fulfillment not converged to shipped: $after" >&2
exit 1

