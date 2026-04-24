#!/bin/bash
set -euo pipefail

# Smoke: enqueue an invitation send job and wait for the worker to mark it sent/cancelled/error.
#
# Required env:
# - REVIEWS_BASE_URL (e.g. https://api.pivota.cc)
# - MERCHANT_ID
# - ORDER_ID
# - X_INTERNAL_KEY (invitation issuer internal key)
#
# Optional env:
# - TIMEOUT_SECONDS (default 180)
# - POLL_SECONDS (default 10)

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
ORDER_ID="${ORDER_ID:-}"
X_INTERNAL_KEY="${X_INTERNAL_KEY:-}"

TIMEOUT_SECONDS="${TIMEOUT_SECONDS:-180}"
POLL_SECONDS="${POLL_SECONDS:-10}"

if [[ -z "$REVIEWS_BASE_URL" || -z "$MERCHANT_ID" || -z "$ORDER_ID" || -z "$X_INTERNAL_KEY" ]]; then
  echo "ERROR: missing env: REVIEWS_BASE_URL, MERCHANT_ID, ORDER_ID, X_INTERNAL_KEY" >&2
  exit 2
fi

REVIEWS_BASE_URL="${REVIEWS_BASE_URL%/}"

echo "== enqueue send job (force_reschedule) =="
ENQ_RESP="$(curl --http1.1 -sS -H "Content-Type: application/json" -H "X-Internal-Key: $X_INTERNAL_KEY" \
  -d "{\"merchant_id\":\"$MERCHANT_ID\",\"order_id\":\"$ORDER_ID\",\"force_reschedule\":true,\"send_now\":true}" \
  "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/enqueue-send-job")"

python3 -c 'import sys,json; o=json.loads(sys.stdin.read()); print("status=",o.get("status"),"ok=",o.get("ok"),"reason=",o.get("reason"))' <<<"$ENQ_RESP"

ENQ_REASON="$(python3 -c 'import sys,json; o=json.loads(sys.stdin.read()); print((o.get("reason") or "").strip())' <<<"$ENQ_RESP" 2>/dev/null || true)"
if [[ "$ENQ_REASON" == "ALREADY_SENT" ]]; then
  echo "OK (already sent; dedup working)"
  exit 0
fi

deadline=$(( $(date +%s) + TIMEOUT_SECONDS ))

echo "== wait for job status (sent/cancelled/error) =="
while true; do
  now=$(date +%s)
  if (( now > deadline )); then
    echo "ERROR: timeout waiting for job terminal status" >&2
    curl --http1.1 -sS -H "X-Internal-Key: $X_INTERNAL_KEY" \
      "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/jobs/by-order?order_id=$ORDER_ID" \
      | python3 -m json.tool | head -n 120
    exit 1
  fi

  body="$(curl --http1.1 -sS -H "X-Internal-Key: $X_INTERNAL_KEY" \
    "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/jobs/by-order?order_id=$ORDER_ID")"

  status="$(python3 -c 'import json,sys; raw=sys.stdin.read() or "{}"; \
o=json.loads(raw); items=o.get("items") or []; \
print(str((items[0].get("status") if items else "") or ""))' <<<"$body" 2>/dev/null || true)"

  echo "job_status=$status"
  if [[ "$status" == "sent" || "$status" == "cancelled" || "$status" == "error" ]]; then
    echo "$body" | python3 -m json.tool | head -n 120
    echo "OK"
    exit 0
  fi

  sleep "$POLL_SECONDS"
done
