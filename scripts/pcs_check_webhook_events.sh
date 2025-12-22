#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/pcs_check_webhook_events.sh --api-base <https://your-backend> --merchant-id <merch_xxx> [--limit 20] [--auth auto|x-api-key|bearer]

Purpose:
  Verifies whether Shopify webhooks have been ingested by the backend by reading
  `pcs_shopify_webhook_events` via:
    GET /integrations/shopify/webhooks/events

Auth:
  - Default `--auth auto`:
      - Prefer `X-API-Key` against GET /agent/v1/debug/shopify/webhooks/events
      - Fallback to Bearer against GET /integrations/shopify/webhooks/events
  - X-API-Key:
      - Provide via env var `X_API_KEY`, otherwise you'll be prompted (won't echo).
  - Bearer:
      - Provide via env var `PIVOTA_BEARER_TOKEN`, otherwise you'll be prompted (won't echo),
        or press Enter to login via POST /api/auth/login.

Notes:
  - Does NOT print payload_json / PII (endpoint is metadata-only).
  - Requires curl + jq.
USAGE
}

API_BASE=""
MERCHANT_ID=""
LIMIT="20"
AUTH_MODE="auto"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --api-base)
      API_BASE="${2:-}"; shift 2;;
    --merchant-id)
      MERCHANT_ID="${2:-}"; shift 2;;
    --limit)
      LIMIT="${2:-}"; shift 2;;
    --auth)
      AUTH_MODE="${2:-}"; shift 2;;
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
  if [[ "${AUTH_MODE}" == "x-api-key" || "${AUTH_MODE}" == "auto" ]]; then
    if [[ -z "${X_API_KEY:-}" ]]; then
      echo "No X_API_KEY / PIVOTA_BEARER_TOKEN found."
      echo "Tip: input is hidden (you may see a key icon). Paste and press Enter."
      read -s "X_API_KEY?X-API-Key (recommended): "
      echo
      export X_API_KEY
    fi

    if [[ -n "${X_API_KEY:-}" ]]; then
      curl -sS "${API_BASE%/}/agent/v1/debug/shopify/webhooks/events?merchant_id=${MERCHANT_ID}&limit=${LIMIT}" \
        -H "X-API-Key: ${X_API_KEY}" \
        | jq '{status, merchant_id, count: (.events|length), events: (.events | map({id, topic, shop_domain, signature_verified, received_at, occurred_at, chain_hash}))}'
      exit 0
    fi
  fi

  if [[ "${AUTH_MODE}" == "bearer" || "${AUTH_MODE}" == "auto" ]]; then
    echo "No PIVOTA_BEARER_TOKEN found."
    echo "Tip: input is hidden (you may see a key icon). Paste the token and press Enter."
    echo "Alternatively, press Enter to login via ${API_BASE%/}/api/auth/login and fetch a token."
    read -s "PIVOTA_BEARER_TOKEN?Pivota Bearer token (or press Enter): "
    echo
    if [[ -z "${PIVOTA_BEARER_TOKEN:-}" ]]; then
      if [[ -z "${PIVOTA_EMAIL:-}" ]]; then
        read "PIVOTA_EMAIL?Pivota email: "
      fi
      if [[ -z "${PIVOTA_PASSWORD:-}" ]]; then
        read -s "PIVOTA_PASSWORD?Pivota password: "
        echo
      fi

      LOGIN_RESP="$(
        curl -sS "${API_BASE%/}/api/auth/login" \
          -H "Content-Type: application/json" \
          -d "{\"email\":\"${PIVOTA_EMAIL}\",\"password\":\"${PIVOTA_PASSWORD}\"}" \
          || true
      )"

      PIVOTA_BEARER_TOKEN="$(echo "${LOGIN_RESP}" | jq -r '.token // empty')"
      if [[ -z "${PIVOTA_BEARER_TOKEN}" ]]; then
        echo "Failed to login and fetch token from ${API_BASE%/}/api/auth/login." >&2
        echo "Response: ${LOGIN_RESP}" >&2
        exit 1
      fi
      export PIVOTA_BEARER_TOKEN
    else
      export PIVOTA_BEARER_TOKEN
    fi
  fi
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
