#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'USAGE'
Usage:
  scripts/pcs_trigger_shopify_webhook.sh --shop-domain <shop.myshopify.com> --merchant-id <merch_xxx> --api-base <https://your-backend>

Purpose:
  Triggers a real Shopify webhook delivery (orders/updated) by updating a recent order's note.
  This validates:
    - Shopify -> Pivota webhook delivery
    - Pivota webhook HMAC verification is configured correctly (SHOPIFY_CLIENT_SECRET)

Notes:
  - Requires curl + jq.
  - Prompts for SHOPIFY_ACCESS_TOKEN (won't echo).
  - Does NOT use X-API-Key. This talks directly to Shopify Admin REST.
USAGE
}

SHOP_DOMAIN=""
MERCHANT_ID=""
API_BASE=""
ORDER_ID=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --shop-domain)
      SHOP_DOMAIN="${2:-}"; shift 2;;
    --merchant-id)
      MERCHANT_ID="${2:-}"; shift 2;;
    --api-base)
      API_BASE="${2:-}"; shift 2;;
    --order-id)
      ORDER_ID="${2:-}"; shift 2;;
    -h|--help)
      usage; exit 0;;
    *)
      echo "Unknown arg: $1" >&2
      usage
      exit 2;;
  esac
done

if [[ -z "${SHOP_DOMAIN}" || -z "${MERCHANT_ID}" || -z "${API_BASE}" ]]; then
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

if [[ -z "${SHOPIFY_ACCESS_TOKEN:-}" ]]; then
  read -r -s -p "Shopify Admin access token: " SHOPIFY_ACCESS_TOKEN
  echo
  export SHOPIFY_ACCESS_TOKEN
fi

echo "Checking registered webhooks that point to: ${API_BASE}/webhooks/shopify/${MERCHANT_ID}"
curl -sS --max-time 20 "https://${SHOP_DOMAIN}/admin/api/2024-07/webhooks.json?limit=250" \
  -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
  | jq -r --arg addr "${API_BASE}/webhooks/shopify/${MERCHANT_ID}" \
    '.webhooks[] | select(.address==$addr) | "\(.id)\t\(.topic)\t\(.address)"' \
  || true

if [[ -z "${ORDER_ID}" ]]; then
  echo "Fetching a recent order (status=any)..."
  ORDER_ID="$(
    curl -sS --max-time 20 "https://${SHOP_DOMAIN}/admin/api/2024-07/orders.json?status=any&limit=1&fields=id,name,updated_at,note,financial_status,fulfillment_status" \
      -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
    | jq -r '.orders[0].id // empty'
  )"
fi

if [[ -z "${ORDER_ID}" ]]; then
  echo "No order found in Shopify. Create a test order first, then re-run." >&2
  exit 1
fi

echo "Using order_id=${ORDER_ID}"
ORDER_BEFORE="$(
  curl -sS --max-time 20 "https://${SHOP_DOMAIN}/admin/api/2024-07/orders/${ORDER_ID}.json?fields=id,name,updated_at,note" \
    -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
  | jq -c '.order'
)"
echo "Before: ${ORDER_BEFORE}"

NOW_ISO="$(date -u +"%Y-%m-%dT%H:%M:%SZ")"
NEW_NOTE="PCS webhook test @ ${NOW_ISO} (trigger orders/updated)"

echo "Updating order note to trigger orders/updated webhook..."
curl -sS --max-time 20 "https://${SHOP_DOMAIN}/admin/api/2024-07/orders/${ORDER_ID}.json" \
  -X PUT \
  -H "X-Shopify-Access-Token: ${SHOPIFY_ACCESS_TOKEN}" \
  -H "Content-Type: application/json" \
  -d "{\"order\":{\"id\":${ORDER_ID},\"note\":\"${NEW_NOTE}\"}}" \
  | jq -c '.order | {id, name, updated_at, note}'

echo "Done."
echo
echo "Next:"
echo "  - In Shopify Admin, open Settings -> Notifications -> Webhooks (or the app/webhooks page)."
echo "  - Find the 'orders/updated' webhook pointing to ${API_BASE}/webhooks/shopify/${MERCHANT_ID}."
echo "  - Check the latest delivery: expected HTTP 2xx from your backend (not 401/403)."
