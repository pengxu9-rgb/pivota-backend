#!/usr/bin/env bash
set -euo pipefail

SHOP_DOMAIN="${1:-${SHOP_DOMAIN:-}}"
MERCHANT_TOKEN="${2:-${MERCHANT_TOKEN:-}}"
BASE_URL="${BASE_URL:-http://localhost:8000}"

if [[ -z "${SHOP_DOMAIN}" || -z "${MERCHANT_TOKEN}" ]]; then
  echo "Usage: $0 <shop.myshopify.com> <merchant_jwt_token>" >&2
  echo "Or set env SHOP_DOMAIN=... MERCHANT_TOKEN=... [BASE_URL=...]" >&2
  exit 2
fi

resp="$(curl -sS \
  -H "Authorization: Bearer ${MERCHANT_TOKEN}" \
  "${BASE_URL%/}/integrations/shopify/oauth/start?shop=${SHOP_DOMAIN}")"

printf '%s' "$resp" | python3 - <<'PY'
import json
import sys

raw = sys.stdin.read()
try:
    data = json.loads(raw)
except Exception:
    print(raw)
    raise SystemExit(1)

if data.get("status") != "success":
    print(json.dumps(data, ensure_ascii=False))
    raise SystemExit(1)

url = data.get("authorization_url") or ""
if not url:
    print(json.dumps(data, ensure_ascii=False))
    raise SystemExit(1)

print(url)
PY
