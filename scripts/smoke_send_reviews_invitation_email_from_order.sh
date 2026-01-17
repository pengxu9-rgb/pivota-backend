#!/bin/bash
set -euo pipefail

# Smoke: send a buyer review invitation email for a specific paid order (internal).
#
# Required env:
# - BASE_URL or REVIEWS_BASE_URL
# - MERCHANT_ID
# - ORDER_ID
#
# Optional env:
# - TTL_SECONDS (default 86400)
# - MAX_LINKS (default 3)
# - X_INTERNAL_KEY (otherwise prompt)
#
# This script does not print invitation_token or buyer email.

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-${BASE_URL:-}}"
MERCHANT_ID="${MERCHANT_ID:-}"
ORDER_ID="${ORDER_ID:-}"
TTL_SECONDS="${TTL_SECONDS:-86400}"
MAX_LINKS="${MAX_LINKS:-3}"

if [[ -z "$REVIEWS_BASE_URL" || -z "$MERCHANT_ID" || -z "$ORDER_ID" ]]; then
  echo "ERROR: missing env: REVIEWS_BASE_URL (or BASE_URL), MERCHANT_ID, ORDER_ID" >&2
  exit 2
fi
REVIEWS_BASE_URL="${REVIEWS_BASE_URL%/}"

X_INTERNAL_KEY="${X_INTERNAL_KEY:-}"
if [[ -z "$X_INTERNAL_KEY" ]]; then
  read -r -s -p "X-Internal-Key (invitation issuer): " X_INTERNAL_KEY
  echo
fi
[[ -n "$X_INTERNAL_KEY" ]] || { echo "ERROR: empty X_INTERNAL_KEY" >&2; exit 2; }

BODY="$(python3 -c 'import json,os
print(json.dumps({
  "merchant_id": os.environ["MERCHANT_ID"],
  "order_id": os.environ["ORDER_ID"],
  "ttl_seconds": int(os.environ.get("TTL_SECONDS","86400")),
  "max_links": int(os.environ.get("MAX_LINKS","3")),
}, separators=(",",":")))' )"

tmp_body="$(mktemp "${TMPDIR:-/tmp}/reviews_inv_send_body.XXXXXX")"
tmp_hdr="$(mktemp "${TMPDIR:-/tmp}/reviews_inv_send_hdr.XXXXXX")"

http_status="$(curl --http1.1 --max-time 20 -sS -D "$tmp_hdr" -o "$tmp_body" -w "%{http_code}" \
  -H "Content-Type: application/json" \
  -H "X-Internal-Key: $X_INTERNAL_KEY" \
  -d "$BODY" \
  "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/send-email-from-order" || true)"

resp="$(cat "$tmp_body" 2>/dev/null || true)"

if [[ "$http_status" != "200" ]]; then
  echo "ERROR: send failed (http_status=$http_status)" >&2
  echo "--- headers (first 20) ---" >&2
  head -n 20 "$tmp_hdr" >&2 || true
  if [[ -n "$resp" ]]; then
    echo "--- body (first 512B) ---" >&2
    printf '%s' "$resp" | head -c 512 >&2 || true
  fi
  rm -f "$tmp_body" "$tmp_hdr" || true
  exit 1
fi

sent="$(printf '%s' "$resp" | python3 -c 'import sys,json; o=json.load(sys.stdin); print(str(bool(o.get("sent", True))).lower())' 2>/dev/null || true)"
reason="$(printf '%s' "$resp" | python3 -c 'import sys,json; o=json.load(sys.stdin); print((o.get("reason") or ""))' 2>/dev/null || true)"
subject_count="$(printf '%s' "$resp" | python3 -c 'import sys,json; o=json.load(sys.stdin); print(int(o.get("subject_count") or 0))' 2>/dev/null || true)"

echo "send_ok sent=$sent subject_count=${subject_count:-0} reason=${reason:-}"
echo "x_link_configured=$(grep -i '^X-Reviews-Invitation-Link-Configured:' \"$tmp_hdr\" | tail -n 1 | awk '{print $2}' | tr -d '\\r' || true)"
echo "x_link_base=$(grep -i '^X-Reviews-Invitation-Link-Base:' \"$tmp_hdr\" | tail -n 1 | cut -d' ' -f2- | tr -d '\\r' || true)"

rm -f "$tmp_body" "$tmp_hdr" || true

