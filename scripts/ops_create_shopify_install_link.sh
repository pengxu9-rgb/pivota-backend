#!/usr/bin/env bash
set -euo pipefail

# Create a no-login Shopify install link for a merchant+shop.
#
# Requires an authenticated Pivota JWT (admin/employee, or merchant for self).
#
# Usage:
#   BASE_URL="https://web-production-fedb.up.railway.app" \
#   TOKEN="..." \
#   MERCHANT_ID="merch_..." \
#   SHOP_DOMAIN="your-shop.myshopify.com" \
#   ./scripts/ops_create_shopify_install_link.sh
#

BASE_URL="${BASE_URL:-http://localhost:8000}"
TOKEN="${TOKEN:-${ADMIN_TOKEN:-${MERCHANT_TOKEN:-}}}"
MERCHANT_ID="${MERCHANT_ID:-}"
SHOP_DOMAIN="${SHOP_DOMAIN:-}"
TTL_SECONDS="${TTL_SECONDS:-}"

if [[ -z "${TOKEN}" ]]; then
  echo "ERROR: missing TOKEN (or ADMIN_TOKEN / MERCHANT_TOKEN)" >&2
  exit 2
fi
if [[ -z "${MERCHANT_ID}" ]]; then
  echo "ERROR: missing MERCHANT_ID" >&2
  exit 2
fi
if [[ -z "${SHOP_DOMAIN}" ]]; then
  echo "ERROR: missing SHOP_DOMAIN (e.g. your-shop.myshopify.com)" >&2
  exit 2
fi

payload="$(python3 - <<PY
import json, os
body = {"merchant_id": os.environ["MERCHANT_ID"], "shop_domain": os.environ["SHOP_DOMAIN"]}
ttl = os.environ.get("TTL_SECONDS","").strip()
if ttl:
    try:
        body["ttl_seconds"] = int(ttl)
    except Exception:
        pass
print(json.dumps(body))
PY
)"

resp="$(curl -sS -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  --data "$payload" \
  "${BASE_URL%/}/integrations/shopify/install-links")"

RESP="$resp" python3 - <<'PY'
import json
import os
import sys

raw = os.environ.get("RESP", "")
try:
    data = json.loads(raw)
except Exception:
    print(raw)
    raise SystemExit(1)

if data.get("status") != "success":
    print(json.dumps(data, ensure_ascii=False))
    raise SystemExit(1)

print(data.get("install_url") or "")
PY
