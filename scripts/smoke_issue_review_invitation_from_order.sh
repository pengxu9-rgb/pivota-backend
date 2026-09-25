#!/bin/bash
set -euo pipefail

# Smoke: issue invitation_token from a paid order (internal), then exchange -> create -> approve -> visible.
#
# Env:
# - REVIEWS_BASE_URL (required)
# - PROOF_ISSUER_BASE_URL (optional; used only for /__build sanity)
# - MERCHANT_ID (required)
# - ORDER_ID (required)
# - PLATFORM_PRODUCT_ID (optional; if omitted, uses the first subject from the issued invitation)
# - VARIANT_ID (optional; only used when PLATFORM_PRODUCT_ID is provided)
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

if [[ -z "$REVIEWS_BASE_URL" || -z "$MERCHANT_ID" || -z "$ORDER_ID" ]]; then
  echo "ERROR: missing env: REVIEWS_BASE_URL, MERCHANT_ID, ORDER_ID" >&2
  exit 2
fi

REVIEWS_BASE_URL="${REVIEWS_BASE_URL%/}"

X_INTERNAL_KEY="${X_INTERNAL_KEY:-}"
if [[ -z "$X_INTERNAL_KEY" ]]; then
  read -r -s -p "X-Internal-Key (invitation issuer): " X_INTERNAL_KEY
  echo
fi
[[ -n "$X_INTERNAL_KEY" ]] || { echo "ERROR: empty X_INTERNAL_KEY" >&2; exit 2; }

JWT_SECRET_KEY="${JWT_SECRET_KEY:-}"
if [[ -z "$JWT_SECRET_KEY" ]]; then
  read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " JWT_SECRET_KEY
  echo
fi
[[ -n "$JWT_SECRET_KEY" ]] || { echo "ERROR: empty JWT_SECRET_KEY" >&2; exit 2; }

echo "== issue invitation_token from order =="
ISSUE_BODY="$(python3 -c 'import json,os
mid=os.environ["MERCHANT_ID"]
oid=os.environ["ORDER_ID"]
pp=(os.environ.get("PLATFORM_PRODUCT_ID") or "").strip()
vid=(os.environ.get("VARIANT_ID") or "").strip()
body={"merchant_id":mid,"order_id":oid,"ttl_seconds":86400}
if pp:
  body["platform_product_id"]=pp
  if vid:
    body["variant_id"]=vid
print(json.dumps(body,separators=(",",":")))' )"
_tmp_issue_body="$(mktemp "${TMPDIR:-/tmp}/reviews_inv_issue_body.XXXXXX")"
_tmp_issue_hdr="$(mktemp "${TMPDIR:-/tmp}/reviews_inv_issue_hdr.XXXXXX")"
ISSUE_HTTP="$(curl --http1.1 --max-time 20 -sS -D "$_tmp_issue_hdr" -o "$_tmp_issue_body" -w "%{http_code}" \
  -H "Content-Type: application/json" -H "X-Internal-Key: $X_INTERNAL_KEY" \
  -d "$ISSUE_BODY" \
  "$REVIEWS_BASE_URL/internal/reviews/v1/invitation/issue-from-order" || true)"
ISSUE_RESP="$(cat "$_tmp_issue_body" 2>/dev/null || true)"

INV_FP="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json,hashlib; o=json.load(sys.stdin); t=o.get("invitation_token",""); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else "")' 2>/dev/null || true)"
if [[ "$ISSUE_HTTP" != "200" || -z "$INV_FP" ]]; then
  echo "ERROR: failed to issue invitation_token (http_status=${ISSUE_HTTP:-})" >&2
  echo "--- headers (first 20) ---" >&2
  head -n 20 "$_tmp_issue_hdr" >&2 || true
  if [[ -n "$ISSUE_RESP" ]]; then
    if printf '%s' "$ISSUE_RESP" | head -c 1 | grep -q '{'; then
      echo "--- body keys/detail ---" >&2
      printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json
o=json.load(sys.stdin)
print("keys=", sorted(list(o.keys()))[:20])
print("detail=", o.get("detail"))
err=o.get("error") or {}
if isinstance(err, dict):
  print("error.code=", err.get("code"))
  print("error.message=", err.get("message"))' >&2 || true
    else
      echo "--- body (first 256B) ---" >&2
      printf '%s' "$ISSUE_RESP" | head -c 256 >&2 || true
    fi
  fi
  rm -f "$_tmp_issue_body" "$_tmp_issue_hdr" || true
  exit 1
fi
echo "issue_ok invitation_fp=$INV_FP"
rm -f "$_tmp_issue_body" "$_tmp_issue_hdr" || true

echo "== select subject for downstream checks =="
SUBJECT_JSON="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json
o=json.load(sys.stdin)
if isinstance(o.get("subject"), dict):
  print(json.dumps(o["subject"], separators=(",",":")))
  raise SystemExit(0)
items=o.get("items") or []
if isinstance(items, list) and items and isinstance(items[0], dict):
  subj=items[0].get("subject")
  if isinstance(subj, dict):
    print(json.dumps(subj, separators=(",",":")))
    raise SystemExit(0)
print("")' 2>/dev/null || true)"
if [[ -z "$SUBJECT_JSON" ]]; then
  echo "ERROR: failed to extract subject from invitation response" >&2
  exit 1
fi
echo "subject=$SUBJECT_JSON"

PLATFORM_PRODUCT_ID_USED="$(printf '%s' "$SUBJECT_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); print((o.get("platform_product_id") or "").strip())' 2>/dev/null || true)"
VARIANT_ID_USED="$(printf '%s' "$SUBJECT_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); print((o.get("variant_id") or "").strip())' 2>/dev/null || true)"
PLATFORM_USED="$(printf '%s' "$SUBJECT_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); print((o.get("platform") or "shopify").strip())' 2>/dev/null || true)"
[[ -n "$PLATFORM_PRODUCT_ID_USED" ]] || { echo "ERROR: subject missing platform_product_id" >&2; exit 1; }
echo "subject_platform=$PLATFORM_USED platform_product_id=$PLATFORM_PRODUCT_ID_USED variant_id=${VARIANT_ID_USED:-<none>}"

echo "== exchange invitation -> submission_token =="
INVITATION_TOKEN="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("invitation_token",""))' 2>/dev/null || true)"
[[ -n "$INVITATION_TOKEN" ]] || { echo "ERROR: missing invitation_token in response" >&2; exit 1; }
EXCHANGE_RESP="$(curl --http1.1 --max-time 20 -sS -H "Authorization: Bearer $INVITATION_TOKEN" \
  -H "Content-Type: application/json" -d '{"ttl_seconds":900}' \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange")"

SUB_FP="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json,hashlib; o=json.load(sys.stdin); t=o.get("submission_token",""); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else "")' 2>/dev/null || true)"
[[ -z "$SUB_FP" ]] && { echo "ERROR: failed to exchange invitation_token" >&2; exit 1; }
echo "exchange_ok submission_token_fp=$SUB_FP"

SUBMISSION_TOKEN="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("submission_token",""))' 2>/dev/null || true)"
[[ -n "$SUBMISSION_TOKEN" ]] || { echo "ERROR: missing submission_token in exchange response" >&2; exit 1; }

echo "== create buyer review (under_review) =="
IDEMPOTENCY_KEY="$(python3 -c 'import os,base64; print(base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("="))')"
CREATE_BODY="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM_PRODUCT_ID_USED="$PLATFORM_PRODUCT_ID_USED" VARIANT_ID_USED="$VARIANT_ID_USED" PLATFORM_USED="$PLATFORM_USED" python3 -c 'import json,os
mid=os.environ["MERCHANT_ID"]
pp=os.environ["PLATFORM_PRODUCT_ID_USED"]
platform=(os.environ.get("PLATFORM_USED") or "shopify").strip() or "shopify"
vid=(os.environ.get("VARIANT_ID_USED") or "").strip()
body={"merchant_id":mid,"platform":platform,"platform_product_id":pp,"rating":5,"title":"Works","body":"Works as expected."}
if vid:
  body["variant_id"]=vid
print(json.dumps(body,separators=(",",":")))' )"
PLATFORM_PRODUCT_ID_USED="$PLATFORM_PRODUCT_ID_USED" VARIANT_ID_USED="$VARIANT_ID_USED" PLATFORM_USED="$PLATFORM_USED" \
CREATE_RESP="$(curl --http1.1 --max-time 20 -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -H "Content-Type: application/json" \
  -d "$CREATE_BODY" \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/reviews")"

REVIEW_ID="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(str(json.load(sys.stdin).get("review_id","")))' 2>/dev/null || true)"
[[ -z "$REVIEW_ID" ]] && { echo "ERROR: create review failed" >&2; exit 1; }
echo "review_id=$REVIEW_ID"

echo "== approve -> active (employee) =="
EMP_TOKEN="$(JWT_SECRET_KEY="$JWT_SECRET_KEY" EMPLOYEE_ID="emp_smoke" EMAIL="ops+reviews@pivota.invalid" python3 -c 'import os,base64,hmac,hashlib,json,time
def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
now=int(time.time())
hdr={"alg":"HS256","typ":"JWT"}
pl={"sub":os.environ["EMPLOYEE_ID"],"user_id":os.environ["EMPLOYEE_ID"],"employee_id":os.environ["EMPLOYEE_ID"],
    "email":os.environ["EMAIL"],"role":"admin","permissions":["reviews.read","reviews.moderate.list","reviews.moderate.status"],
    "iat":now,"exp":now+3600}
msg=b64u(json.dumps(hdr,separators=(",",":")).encode())+"."+b64u(json.dumps(pl,separators=(",",":")).encode())
sig=b64u(hmac.new(os.environ["JWT_SECRET_KEY"].encode(), msg.encode(), hashlib.sha256).digest())
print(msg+"."+sig)')"

curl --http1.1 --max-time 20 -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"smoke"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
echo "approved_ok"

echo "== read path visible? =="
CHECK_BODY="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM_PRODUCT_ID_USED="$PLATFORM_PRODUCT_ID_USED" VARIANT_ID_USED="$VARIANT_ID_USED" PLATFORM_USED="$PLATFORM_USED" python3 -c 'import json,os
mid=os.environ["MERCHANT_ID"]
pp=os.environ["PLATFORM_PRODUCT_ID_USED"]
platform=(os.environ.get("PLATFORM_USED") or "shopify").strip() or "shopify"
vid=(os.environ.get("VARIANT_ID_USED") or "").strip()
sku={"merchant_id":mid,"platform":platform,"platform_product_id":pp}
if vid:
  sku["variant_id"]=vid
body={"operation":"list_sku_reviews","payload":{"sku":sku,"filters":{"limit":50}}}
print(json.dumps(body,separators=(",",":")))' )"
PLATFORM_PRODUCT_ID_USED="$PLATFORM_PRODUCT_ID_USED" VARIANT_ID_USED="$VARIANT_ID_USED" PLATFORM_USED="$PLATFORM_USED" \
CHECK_RESP="$(curl --http1.1 --max-time 20 -sS -H "Content-Type: application/json" \
  -d "$CHECK_BODY" \
  "$REVIEWS_BASE_URL/agent/shop/v1/invoke")"

FOUND="$(printf '%s' "$CHECK_RESP" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=str(sys.argv[1]); items=o.get("items") or []; print("found" if any(str(it.get("review_id"))==rid for it in items) else "not_found")' "$REVIEW_ID" 2>/dev/null || true)"
echo "$FOUND"
[[ "$FOUND" == "found" ]] && echo "OK" || exit 1
