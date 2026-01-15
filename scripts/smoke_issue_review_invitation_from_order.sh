#!/bin/bash
set -euo pipefail

# Smoke: issue invitation_token from a paid order (internal), then exchange -> create -> approve -> visible.
#
# Env:
# - REVIEWS_BASE_URL (required)
# - PROOF_ISSUER_BASE_URL (optional; used only for /__build sanity)
# - MERCHANT_ID (required)
# - ORDER_ID (required)
# - PLATFORM_PRODUCT_ID (required; used for read-path visibility check)
# - VARIANT_ID (optional)
#
# Prompts:
# - X_INTERNAL_KEY: internal key for /internal/reviews/v1/invitation/issue-from-order
# - JWT_SECRET_KEY: employee JWT signing secret (to approve review)
#
# Notes:
# - This script never prints secrets/tokens; it prints short fingerprints only.

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-${BASE_URL:-}}"
MERCHANT_ID="${MERCHANT_ID:-}"
ORDER_ID="${ORDER_ID:-}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"

if [[ -z "$REVIEWS_BASE_URL" || -z "$MERCHANT_ID" || -z "$ORDER_ID" || -z "$PLATFORM_PRODUCT_ID" ]]; then
  echo "ERROR: missing env: REVIEWS_BASE_URL, MERCHANT_ID, ORDER_ID, PLATFORM_PRODUCT_ID" >&2
  exit 2
fi

REVIEWS_BASE_URL="${REVIEWS_BASE_URL%/}"

read -rs "X_INTERNAL_KEY?X-Internal-Key (invitation issuer): " ; echo
read -rs "JWT_SECRET_KEY?JWT_SECRET_KEY (reviews backend Railway env): " ; echo

echo "== issue invitation_token from order =="
ISSUE_RESP="$(curl -sS -H "Content-Type: application/json" -H "X-Internal-Key: $X_INTERNAL_KEY" \
  -d "{\"merchant_id\":\"$MERCHANT_ID\",\"order_id\":\"$ORDER_ID\",\"ttl_seconds\":86400}" \
  "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/issue-from-order")"

INV_FP="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json,hashlib; o=json.load(sys.stdin); t=o.get("invitation_token",""); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else "")')"
[[ -z "$INV_FP" ]] && { echo "ERROR: failed to issue invitation_token" >&2; exit 1; }
echo "issue_ok invitation_fp=$INV_FP"

echo "== exchange invitation -> submission_token =="
EXCHANGE_RESP="$(curl -sS -H "Authorization: Bearer $(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"invitation_token\",\"\"))')" \
  -H "Content-Type: application/json" -d '{"ttl_seconds":900}' \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange")"

SUB_FP="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json,hashlib; o=json.load(sys.stdin); t=o.get("submission_token",""); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else "")')"
[[ -z "$SUB_FP" ]] && { echo "ERROR: failed to exchange invitation_token" >&2; exit 1; }
echo "exchange_ok submission_token_fp=$SUB_FP"

SUBMISSION_TOKEN="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"submission_token\",\"\"))')"

echo "== create buyer review (under_review) =="
IDEMPOTENCY_KEY="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip(\"=\"))')"
CREATE_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -H "Content-Type: application/json" \
  -d "{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"shopify\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\"$( [[ -n "$VARIANT_ID" ]] && printf ',\"variant_id\":\"%s\"' "$VARIANT_ID" ),\"rating\":5,\"title\":\"Works\",\"body\":\"Works as expected.\"}" \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/reviews")"

REVIEW_ID="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get(\"review_id\", \"\"))')"
[[ -z "$REVIEW_ID" ]] && { echo "ERROR: create review failed" >&2; exit 1; }
echo "review_id=$REVIEW_ID"

echo "== approve -> active (employee) =="
EMP_TOKEN="$(JWT_SECRET_KEY="$JWT_SECRET_KEY" EMPLOYEE_ID="emp_smoke" EMAIL="redacted" python3 -c 'import os,base64,hmac,hashlib,json,time\n\ndef b64u(b):\n  return base64.urlsafe_b64encode(b).decode().rstrip(\"=\")\n\nnow=int(time.time())\nhdr={\"alg\":\"HS256\",\"typ\":\"JWT\"}\npl={\"sub\":os.environ[\"EMPLOYEE_ID\"],\"user_id\":os.environ[\"EMPLOYEE_ID\"],\"employee_id\":os.environ[\"EMPLOYEE_ID\"],\n    \"email\":os.environ[\"EMAIL\"],\"role\":\"admin\",\"permissions\":[\"reviews.moderate\"],\"iat\":now,\"exp\":now+3600}\nbody=b64u(json.dumps(hdr,separators=(\",\",\":\")).encode())+\".\"+b64u(json.dumps(pl,separators=(\",\",\":\")).encode())\nsig=b64u(hmac.new(os.environ[\"JWT_SECRET_KEY\"].encode(), body.encode(), hashlib.sha256).digest())\nprint(body+\".\"+sig)')"

curl -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"smoke"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
echo "approved_ok"

echo "== read path visible? =="
CHECK_RESP="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"shopify\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\"$( [[ -n "$VARIANT_ID" ]] && printf ',\"variant_id\":\"%s\"' "$VARIANT_ID" )},\"filters\":{\"limit\":50}}}" \
  "$REVIEWS_BASE_URL/agent/shop/v1/invoke")"

FOUND="$(printf '%s' "$CHECK_RESP" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=str(sys.argv[1]); items=o.get(\"items\") or []; print(\"found\" if any(str(it.get(\"review_id\"))==rid for it in items) else \"not_found\")' "$REVIEW_ID")"
echo "$FOUND"
[[ "$FOUND" == "found" ]] && echo "OK" || exit 1
