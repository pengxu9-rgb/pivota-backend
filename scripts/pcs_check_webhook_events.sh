#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/pcs_check_webhook_events.sh --api-base <https://your-backend> --merchant-id <merch_xxx> [--limit 20]

Purpose:
  Verifies whether Shopify webhooks have been ingested by the backend by reading
  `pcs_shopify_webhook_events` via:
    GET /integrations/shopify/webhooks/events

Auth:
  - Requires an app Bearer token (merchant/employee/admin).
  - Provide it via env var `PIVOTA_BEARER_TOKEN`, otherwise you'll be prompted (won't echo).

Notes:
  - Does NOT print payload_json / PII (endpoint is metadata-only).
  - Requires curl + jq.
USAGE
}

API_BASE=""
MERCHANT_ID=""
LIMIT="20"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base)
      API_BASE="${2:-}"; shift 2;;
    --merchant-id)
      MERCHANT_ID="${2:-}"; shift 2;;
    --limit)
      LIMIT="${2:-}"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

if [[ -z "${API_BASE}" || -z "${MERCHANT_ID}" ]]; then
  usage
  exit 2
fi

if ! command -v curl >/dev/null 2>&1; then
  echo "Missing dependency: curl" >&2
  exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "Missing dependency: jq" >&2
  exit 1
fi

if [[ -z "${PIVOTA_BEARER_TOKEN:-}" ]]; then
  read -s "PIVOTA_BEARER_TOKEN?Pivota Bearer token: "
  echo
  export PIVOTA_BEARER_TOKEN
fi

SAFE_LIMIT="$(python3 - <<PY
import os
try:
  n=int(os.environ.get("LIMIT","20"))
except Exception:
  n=20
print(max(1,min(n,200)))
PY
)"

curl -sS "${API_BASE%/}/integrations/shopify/webhooks/events?merchant_id=${MERCHANT_ID}&limit=${SAFE_LIMIT}" \
  -H "Authorization: Bearer ${PIVOTA_BEARER_TOKEN}" \
  | jq '{status, merchant_id, count: (.events|length), events: (.events | map({id, topic, shop_domain, signature_verified, received_at, occurred_at, chain_hash}))}'

