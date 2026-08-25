#!/bin/bash
set -euo pipefail

BASE_URL="${BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
AGENT_ID="${AGENT_ID:-smoke}"
SURFACE="${SURFACE:-TERMINAL}"

EXPECT_WRITE_ALLOWED="${EXPECT_WRITE_ALLOWED:-}"

if [[ -z "$BASE_URL" ]]; then
  echo "ERROR: missing BASE_URL (e.g. https://<host>)" >&2
  exit 1
fi
if [[ -z "$MERCHANT_ID" ]]; then
  echo "ERROR: missing MERCHANT_ID" >&2
  exit 1
fi
if [[ -z "$PLATFORM_PRODUCT_ID" ]]; then
  echo "ERROR: missing PLATFORM_PRODUCT_ID" >&2
  exit 1
fi

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }

echo "== __build (best-effort) =="
curl -sS "$BASE_URL/__build" 2>/dev/null | head -c 400 || true
echo

echo "== openapi paths present? =="
# /openapi.json is admin-gated in production - it is the full internal route list. Without a key
# the response is 404 (deliberately, so an anonymous caller cannot even confirm it exists), which
# would surface here as a JSON parse error rather than an auth problem.
ADMIN_KEY="${ADMIN_API_KEY:-${PROMOTIONS_ADMIN_KEY:-}}"
[ -n "$ADMIN_KEY" ] || echo "   note: ADMIN_API_KEY not set - expect 404 from /openapi.json in production" >&2
curl -sS ${ADMIN_KEY:+-H "X-ADMIN-KEY: $ADMIN_KEY"} "$BASE_URL/openapi.json" \
| python3 -c 'import sys,json; p=json.load(sys.stdin).get("paths",{}); want=["/buyer/reviews/v1/verification/issue-token","/buyer/reviews/v1/verification/exchange","/buyer/reviews/v1/reviews","/buyer/reviews/v1/reviews/{review_id}","/buyer/reviews/v1/reviews/{review_id}/media","/employee/reviews/v1/moderation/reviews","/agent/shop/v1/invoke","/agent/shop/v1/review-media/{public_id}"]; miss=[x for x in want if x not in p]; print("all_present" if not miss else "missing="+",".join(miss))'

SUBJECT_JSON="$(python3 -c 'import json,os; mid=os.environ["MERCHANT_ID"]; p=os.environ["PLATFORM"]; pp=os.environ["PLATFORM_PRODUCT_ID"]; vid=os.environ.get("VARIANT_ID","").strip(); s={"merchant_id":mid,"platform":p,"platform_product_id":pp}; 
if vid: s["variant_id"]=vid
print(json.dumps(s))')"

echo
echo "== agent invoke: list_review_entrypoints =="
ENTRY_JSON="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"list_review_entrypoints\",\"payload\":{\"agent_id\":\"$AGENT_ID\",\"surface\":\"$SURFACE\",\"subject\":$SUBJECT_JSON}}" \
  "$BASE_URL/agent/shop/v1/invoke")"
printf '%s\n' "$ENTRY_JSON" | python3 -m json.tool | head -n 120

WRITE_ALLOWED="$(printf '%s' "$ENTRY_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); items=o.get("items") or []; w=[x for x in items if x.get("entrypoint_id")=="PDP_WRITE_REVIEW"]; print("true" if (w and w[0].get("allowed")) else "false")')"
WRITE_REASON="$(printf '%s' "$ENTRY_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); items=o.get("items") or []; w=[x for x in items if x.get("entrypoint_id")=="PDP_WRITE_REVIEW"]; print((w[0].get("reason") if w else ""))')"
echo "write_allowed=$WRITE_ALLOWED reason=$WRITE_REASON"

if [[ -n "$EXPECT_WRITE_ALLOWED" ]]; then
  if [[ "$EXPECT_WRITE_ALLOWED" != "$WRITE_ALLOWED" ]]; then
    echo "ERROR: EXPECT_WRITE_ALLOWED=$EXPECT_WRITE_ALLOWED but got $WRITE_ALLOWED (reason=$WRITE_REASON)" >&2
    exit 1
  fi
fi

echo
echo "== agent invoke: resolve_review_intent (write) =="
RESOLVE_JSON="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"resolve_review_intent\",\"payload\":{\"agent_id\":\"$AGENT_ID\",\"surface\":\"$SURFACE\",\"entrypoint_id\":\"PDP_WRITE_REVIEW\",\"intent\":\"write\",\"subject\":$SUBJECT_JSON}}" \
  "$BASE_URL/agent/shop/v1/invoke")"
printf '%s\n' "$RESOLVE_JSON" | python3 -m json.tool | head -n 120

echo
echo "OK"

