#!/usr/bin/env bash
set -euo pipefail

# Bind a Pivota order to a specific Shopify store connection (merchant_stores.store_id).
#
# Why:
# - Shopify access tokens are shop-scoped.
# - If a merchant connects multiple Shopify stores, using "primary store" for old orders can cause 401s.
#
# Usage:
#   DATABASE_URL="postgresql://..." \
#   ORDER_ID="ORD_..." \
#   SHOP_DOMAIN="pivota-market.myshopify.com" \
#   /bin/bash scripts/ops_bind_order_to_shopify_store.sh
#
# Optional:
#   FORCE=1  # overwrite existing orders.store_id
#

if [[ -z "${DATABASE_URL:-}" ]]; then
  echo "ERROR: missing env DATABASE_URL" >&2
  exit 1
fi
if [[ -z "${ORDER_ID:-}" ]]; then
  echo "ERROR: missing env ORDER_ID" >&2
  exit 1
fi
if [[ -z "${SHOP_DOMAIN:-}" ]]; then
  echo "ERROR: missing env SHOP_DOMAIN (e.g. pivota-market.myshopify.com)" >&2
  exit 1
fi

PSQL_BIN="${PSQL_BIN:-/opt/homebrew/opt/libpq/bin/psql}"
if [[ ! -x "$PSQL_BIN" ]]; then
  echo "ERROR: psql not found at $PSQL_BIN (set PSQL_BIN=/path/to/psql)" >&2
  exit 1
fi

FORCE="${FORCE:-0}"

merchant_id="${MERCHANT_ID:-}"
existing_store_id=""

row="$("$PSQL_BIN" "$DATABASE_URL" -X -tA -c \
  "SELECT
     COALESCE(merchant_id,'') || '|' || COALESCE(store_id,'')
   FROM orders
   WHERE order_id='${ORDER_ID}'
   LIMIT 1;")"

if [[ -z "$row" ]]; then
  echo "ERROR: order not found: $ORDER_ID" >&2
  exit 1
fi

if [[ -z "$merchant_id" ]]; then
  merchant_id="${row%%|*}"
fi
existing_store_id="${row#*|}"

if [[ -z "$merchant_id" ]]; then
  echo "ERROR: order has empty merchant_id (unexpected). Dumping safe order fields:" >&2
  "$PSQL_BIN" "$DATABASE_URL" -X -P pager=off -c \
    "SELECT order_id, merchant_id, store_id, shopify_order_id, status, payment_status, fulfillment_status
     FROM orders WHERE order_id='${ORDER_ID}' LIMIT 1;" >&2
  exit 1
fi

shop_domain_lc="$(python3 - <<'PY'
import os
d=os.environ["SHOP_DOMAIN"].strip().lower()
print(d)
PY
)"

store_id="$("$PSQL_BIN" "$DATABASE_URL" -X -tA -c \
  "SELECT store_id
   FROM merchant_stores
   WHERE merchant_id='${merchant_id}'
     AND platform='shopify'
     AND lower(domain)=lower('${SHOP_DOMAIN}')
     AND status IN ('active','connected')
   ORDER BY connected_at DESC NULLS LAST
   LIMIT 1;")"

if [[ -z "$store_id" ]]; then
  echo "ERROR: no connected Shopify store for merchant_id=$merchant_id with domain=$SHOP_DOMAIN" >&2
  echo "Connected Shopify stores for this merchant:" >&2
  "$PSQL_BIN" "$DATABASE_URL" -X -P pager=off -c \
    "SELECT store_id, domain, status, connected_at
     FROM merchant_stores
     WHERE merchant_id='${merchant_id}' AND platform='shopify'
     ORDER BY connected_at DESC NULLS LAST
     LIMIT 20;" >&2
  echo "" >&2
  echo "Fix: connect that Shopify store first via POST /integrations/shopify/connect, then re-run this script." >&2
  exit 1
fi

if [[ -n "$existing_store_id" && "$FORCE" != "1" ]]; then
  echo "OK: order already has store_id=$existing_store_id (set FORCE=1 to overwrite)" >&2
  "$PSQL_BIN" "$DATABASE_URL" -X -P pager=off -c \
    "SELECT order_id, merchant_id, store_id, shopify_order_id, status, payment_status, fulfillment_status
     FROM orders WHERE order_id='${ORDER_ID}' LIMIT 1;"
  exit 0
fi

"$PSQL_BIN" "$DATABASE_URL" -X -v ON_ERROR_STOP=1 -c \
  "UPDATE orders
   SET store_id='${store_id}', updated_at=NOW()
   WHERE order_id='${ORDER_ID}';" >/dev/null

echo "OK: bound order to store"
echo "order_id=$ORDER_ID"
echo "merchant_id=$merchant_id"
echo "store_id=$store_id"
echo "shop_domain=$SHOP_DOMAIN"

echo ""
"$PSQL_BIN" "$DATABASE_URL" -X -P pager=off -c \
  "SELECT order_id, merchant_id, store_id, shopify_order_id, status, payment_status, fulfillment_status
   FROM orders WHERE order_id='${ORDER_ID}';"
