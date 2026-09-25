#!/bin/bash
set -euo pipefail

# Smoke: mint invitation_token(s) from a paid order via Agent API (no internal issuer key),
# then exchange -> create -> approve -> verify visible on read path.
#
# Env:
# - REVIEWS_BASE_URL (required)  e.g. https://api.pivota.cc
# - ORDER_ID (required)
# - PLATFORM_PRODUCT_ID (optional; if set, request is scoped to this item)
# - VARIANT_ID (optional)
#
# Prompts:
# - X-Checkout-Token (preferred) OR X-API-Key
# - JWT_SECRET_KEY (employee JWT signing secret, to approve review)
#
# Notes:
# - Never prints secrets/tokens; prints short fingerprints only.

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-${BASE_URL:-}}"
ORDER_ID="${ORDER_ID:-}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"

if [[ -z "${REVIEWS_BASE_URL:-}" || -z "${ORDER_ID:-}" ]]; then
  echo "ERROR: missing env: REVIEWS_BASE_URL, ORDER_ID" >&2
  exit 2
fi

REVIEWS_BASE_URL="${REVIEWS_BASE_URL%/}"

CHECKOUT_TOKEN="${CHECKOUT_TOKEN:-}"
API_KEY="${API_KEY:-}"
JWT_SECRET_KEY="${JWT_SECRET_KEY:-}"

if [[ -z "$CHECKOUT_TOKEN" && -z "$API_KEY" ]]; then
  read -r -s -p "X-Checkout-Token (preferred; leave empty to use X-API-Key): " CHECKOUT_TOKEN
  echo
fi
if [[ -z "$CHECKOUT_TOKEN" && -z "$API_KEY" ]]; then
  read -r -s -p "X-API-Key: " API_KEY
  echo
fi
if [[ -z "$JWT_SECRET_KEY" ]]; then
  read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " JWT_SECRET_KEY
  echo
fi
if [[ -z "$CHECKOUT_TOKEN" && -z "$API_KEY" ]]; then
  echo "ERROR: missing auth: X-Checkout-Token or X-API-Key" >&2
  exit 2
fi
[[ -n "$JWT_SECRET_KEY" ]] || { echo "ERROR: empty JWT_SECRET_KEY" >&2; exit 2; }

AUTH_HEADERS=()
if [[ -n "${CHECKOUT_TOKEN:-}" ]]; then
  AUTH_HEADERS+=(-H "X-Checkout-Token: $CHECKOUT_TOKEN")
else
  AUTH_HEADERS+=(-H "X-API-Key: $API_KEY")
fi

REQ_JSON="$(python3 - <<'PY'
import json, os
body = {"ttl_seconds": 86400}
pp = (os.environ.get("PLATFORM_PRODUCT_ID") or "").strip()
vid = (os.environ.get("VARIANT_ID") or "").strip()
if pp:
  body["platform_product_id"] = pp
if vid:
  body["variant_id"] = vid
print(json.dumps(body, separators=(",", ":")))
PY
)"

echo "== issue invitation(s) from order via Agent API =="
ISSUE_RESP="$(curl -sS "${AUTH_HEADERS[@]}" -H "Content-Type: application/json" \
  -d "$REQ_JSON" \
  "$REVIEWS_BASE_URL/agent/v1/orders/$ORDER_ID/reviews/invitations")"

python3 -c 'import json,sys; o=json.load(sys.stdin); assert o.get("status")=="success"' <<<"$ISSUE_RESP" \
  || { echo "ERROR: issue failed" >&2; exit 1; }

INVITATION_TOKEN="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print((o.get("invitation_token") or (((o.get("items") or [{}])[0]) or {}).get("invitation_token") or "").strip())' <<<"$ISSUE_RESP")"

INV_FP="$(python3 -c 'import hashlib,sys; t=sys.stdin.read().strip(); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else \"\")' <<<"$INVITATION_TOKEN")"
[[ -z "$INV_FP" ]] && { echo "ERROR: no invitation_token in response" >&2; exit 1; }
echo "issue_ok invitation_fp=$INV_FP"

SUBJECT_JSON="$(python3 -c 'import json,sys; o=json.load(sys.stdin); sub=(o.get(\"subject\") or (((o.get(\"items\") or [{}])[0]) or {}).get(\"subject\") or {}); print(json.dumps(sub, separators=(\",\",\":\")))' <<<"$ISSUE_RESP")"

MERCHANT_ID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); sub=(o.get(\"subject\") or (((o.get(\"items\") or [{}])[0]) or {}).get(\"subject\") or {}); print((sub.get(\"merchant_id\") or \"\").strip())' <<<"$ISSUE_RESP")"

PLATFORM="$(python3 -c 'import json,sys; o=json.load(sys.stdin); sub=(o.get(\"subject\") or (((o.get(\"items\") or [{}])[0]) or {}).get(\"subject\") or {}); print((sub.get(\"platform\") or \"shopify\").strip())' <<<"$ISSUE_RESP")"

PPID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); sub=(o.get(\"subject\") or (((o.get(\"items\") or [{}])[0]) or {}).get(\"subject\") or {}); print((sub.get(\"platform_product_id\") or \"\").strip())' <<<"$ISSUE_RESP")"

VID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); sub=(o.get(\"subject\") or (((o.get(\"items\") or [{}])[0]) or {}).get(\"subject\") or {}); print(str(sub.get(\"variant_id\") or \"\").strip())' <<<"$ISSUE_RESP")"

if [[ -z "${MERCHANT_ID:-}" || -z "${PPID:-}" ]]; then
  echo "ERROR: missing subject in issue response" >&2
  exit 1
fi

echo "== exchange invitation -> submission_token =="
EXCHANGE_RESP="$(curl -sS -H "Authorization: Bearer $INVITATION_TOKEN" \
  -H "Content-Type: application/json" -d '{"ttl_seconds":900}' \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange")"

SUBMISSION_TOKEN="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(o.get("submission_token",""))' <<<"$EXCHANGE_RESP")"

SUB_FP="$(python3 -c 'import hashlib,sys; t=sys.stdin.read().strip(); print(hashlib.sha256(t.encode()).hexdigest()[:12] if t else \"\")' <<<"$SUBMISSION_TOKEN")"

[[ -z "$SUB_FP" ]] && { echo "ERROR: exchange failed" >&2; exit 1; }
echo "exchange_ok submission_token_fp=$SUB_FP"

echo "== create buyer review (under_review) =="
IDEMPOTENCY_KEY="$(python3 - <<'PY'
import os,base64
print(base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("="))
PY
)"

CREATE_JSON="$(python3 - <<'PY'
import json,os
body={
  "merchant_id": os.environ["MERCHANT_ID"],
  "platform": os.environ["PLATFORM"],
  "platform_product_id": os.environ["PPID"],
  "rating": 5,
  "title": "Works",
  "body": "Works as expected.",
}
vid=(os.environ.get("VID") or "").strip()
if vid:
  body["variant_id"]=vid
print(json.dumps(body,separators=(",",":")))
PY
)"

CREATE_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -H "Content-Type: application/json" -d "$CREATE_JSON" \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/reviews")"

REVIEW_ID="$(python3 -c 'import json,sys; o=json.load(sys.stdin); print(o.get("review_id",""))' <<<"$CREATE_RESP")"
[[ -z "$REVIEW_ID" ]] && { echo "ERROR: create review failed" >&2; exit 1; }
echo "review_id=$REVIEW_ID moderation_state=under_review"

echo "== approve -> active (employee) =="
EMP_TOKEN="$(JWT_SECRET_KEY="$JWT_SECRET_KEY" EMPLOYEE_ID="emp_smoke" EMAIL="redacted" python3 - <<'PY'
import os,base64,hmac,hashlib,json,time

def b64u(b: bytes) -> str:
  return base64.urlsafe_b64encode(b).decode().rstrip("=")

now=int(time.time())
hdr={"alg":"HS256","typ":"JWT"}
pl={"sub":os.environ["EMPLOYEE_ID"],"user_id":os.environ["EMPLOYEE_ID"],"employee_id":os.environ["EMPLOYEE_ID"],
    "email":os.environ["EMAIL"],"role":"admin","permissions":["reviews.moderate"],"iat":now,"exp":now+3600}
body=b64u(json.dumps(hdr,separators=(",",":")).encode())+"."+b64u(json.dumps(pl,separators=(",",":")).encode())
sig=b64u(hmac.new(os.environ["JWT_SECRET_KEY"].encode(), body.encode(), hashlib.sha256).digest())
print(body+"."+sig)
PY
)"

curl -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"smoke"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
echo "approved_ok"

echo "== read path visible? =="
CHECK_JSON="$(python3 - <<'PY'
import json,os
payload={
  "operation":"list_sku_reviews",
  "payload":{
    "sku":{
      "merchant_id":os.environ["MERCHANT_ID"],
      "platform":os.environ["PLATFORM"],
      "platform_product_id":os.environ["PPID"],
    },
    "filters":{"limit":50},
  }
}
vid=(os.environ.get("VID") or "").strip()
if vid:
  payload["payload"]["sku"]["variant_id"]=vid
print(json.dumps(payload,separators=(",",":")))
PY
)"

CHECK_RESP="$(curl -sS -H "Content-Type: application/json" -d "$CHECK_JSON" "$REVIEWS_BASE_URL/agent/shop/v1/invoke")"
FOUND="$(REVIEW_ID="$REVIEW_ID" python3 -c 'import json,sys,os; o=json.load(sys.stdin); rid=os.environ["REVIEW_ID"]; items=o.get("items") or []; print("found" if any(str(it.get("review_id"))==rid for it in items) else "not_found")' <<<"$CHECK_RESP")"
echo "$FOUND"
[[ "$FOUND" == "found" ]] && echo "OK" || exit 1
