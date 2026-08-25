#!/bin/bash
set -euo pipefail

# E2E smoke: proof-issuer invitation -> reviews exchange -> buyer create -> buyer media -> employee approve -> read path visible + media 200/304
#
# This exercises the "invitation_token" path where the browser/client never sees the issuer internal key.
# The reviews backend exchanges an invitation token by calling the proof issuer internally.
#
# Required env:
# - PROOF_ISSUER_BASE_URL (prod: https://proof-issuer-gpx4jyrubq-uw.a.run.app)
# - REVIEWS_BASE_URL      (prod: https://api.pivota.cc)
#   Production is Cloud Run in pivota-prod/us-west1; the old *.up.railway.app
#   hosts are the ROLLBACK and still answer, so a smoke pointed there passes
#   against a platform nobody is served from.
# - MERCHANT_ID
# - PLATFORM_PRODUCT_ID
#
# Optional env:
# - PLATFORM (default: shopify)
# - VARIANT_ID
# - MEDIA_FILE_PATH or MOCK_MEDIA_DIR (default: ~/Desktop/mock_review_media)
# - PROOF_ISSUER_INTERNAL_KEY (otherwise prompts)
# - EMPLOYEE_JWT_SECRET_KEY or JWT_SECRET_KEY (otherwise prompts)

PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
MEDIA_FILE_PATH="${MEDIA_FILE_PATH:-}"

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }

if [[ -z "$PROOF_ISSUER_BASE_URL" ]]; then
  echo "ERROR: missing PROOF_ISSUER_BASE_URL" >&2
  exit 1
fi
if [[ -z "$REVIEWS_BASE_URL" ]]; then
  echo "ERROR: missing REVIEWS_BASE_URL" >&2
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

_pick_media_file() {
  local default_dir="$HOME/Desktop/mock_review_media"
  local candidates_dir="${MOCK_MEDIA_DIR:-$default_dir}"
  if [[ -n "${MEDIA_FILE_PATH:-}" ]]; then
    printf '%s\n' "$MEDIA_FILE_PATH"
    return 0
  fi
  if [[ ! -d "$candidates_dir" ]]; then
    echo "ERROR: MEDIA_FILE_PATH not set and MOCK_MEDIA_DIR not found: $candidates_dir" >&2
    echo "Set MEDIA_FILE_PATH=/abs/path/to/file.jpg (or set MOCK_MEDIA_DIR to your folder)" >&2
    return 1
  fi
  find "$candidates_dir" -maxdepth 2 -type f \( \
    -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' -o -iname '*.gif' -o \
    -iname '*.mp4' -o -iname '*.mov' -o -iname '*.webm' \
  \) 2>/dev/null | head -n 1
}

MEDIA_FILE_PATH="$(_pick_media_file || true)"
if [[ -z "$MEDIA_FILE_PATH" ]]; then
  exit 1
fi
if [[ ! -f "$MEDIA_FILE_PATH" ]]; then
  echo "ERROR: MEDIA_FILE_PATH not found: $MEDIA_FILE_PATH" >&2
  exit 1
fi
echo "media_file=$MEDIA_FILE_PATH"

INTERNAL_KEY="${PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY:-}}}"
if [[ -z "$INTERNAL_KEY" ]]; then
  read -r -s -p "X-Internal-Key (proof issuer): " INTERNAL_KEY
  echo
fi
[[ -n "$INTERNAL_KEY" ]] || { echo "ERROR: empty internal key" >&2; exit 1; }

ISSUE_URL="$PROOF_ISSUER_BASE_URL/internal/reviews/v1/invitation/issue"
EXCHANGE_URL="$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange"
CREATE_URL="$REVIEWS_BASE_URL/buyer/reviews/v1/reviews"

SUBJECT_JSON="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" python3 - <<'PY'
import json, os
mid=os.environ["MERCHANT_ID"]
platform=(os.environ["PLATFORM"] or "").strip().lower()
pp=os.environ["PLATFORM_PRODUCT_ID"]
vid=(os.environ.get("VARIANT_ID") or "").strip()
subject={"merchant_id":mid,"platform":platform,"platform_product_id":pp}
if vid:
  subject["variant_id"]=vid
print(json.dumps({"merchant_id":mid,"subjects":[subject],"verification":"verified_buyer","ttl_seconds":3600}))
PY
)"

echo "== issue invitation token =="
ISSUE_RESP="$(curl -sS -H "Content-Type: application/json" -H "X-Internal-Key: $INTERNAL_KEY" -d "$SUBJECT_JSON" "$ISSUE_URL")"
unset INTERNAL_KEY

INVITATION_TOKEN="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("invitation_token",""))')"
if [[ -z "$INVITATION_TOKEN" ]]; then
  echo "ERROR: no invitation_token from issuer" >&2
  if printf '%s' "$ISSUE_RESP" | grep -q 'UNAUTHORIZED'; then
    echo "HINT: wrong proof-issuer internal key. Use the key from the proof-issuer service (not the web backend invitation key)." >&2
    echo "      You can set PROOF_ISSUER_INTERNAL_KEY (or REVIEWS_PROOF_ISSUER_INTERNAL_KEY)." >&2
  fi
  echo "$ISSUE_RESP" >&2
  exit 1
fi
INV_FP="$(printf '%s' "$INVITATION_TOKEN" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12])')"
echo "issue_ok invitation_fp=$INV_FP"

echo "== exchange invitation -> submission_token =="
EXCHANGE_RESP="$(curl -sS -H "Content-Type: application/json" -H "Authorization: Bearer $INVITATION_TOKEN" -d '{"ttl_seconds":900}' "$EXCHANGE_URL")"
SUBMISSION_TOKEN="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("submission_token",""))')"
EXPIRES_AT="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("expires_at",""))')"
if [[ -z "$SUBMISSION_TOKEN" ]]; then
  echo "ERROR: no submission_token from exchange" >&2
  echo "$EXCHANGE_RESP" >&2
  exit 1
fi
TOKEN_FP="$(printf '%s' "$SUBMISSION_TOKEN" | python3 -c 'import hashlib,sys; print(hashlib.sha256(sys.stdin.read().encode()).hexdigest()[:12])')"
echo "exchange_ok expires_at=$EXPIRES_AT submission_token_fp=$TOKEN_FP"

echo "== create buyer review (under_review) =="
IDEMPOTENCY_KEY="$(python3 - <<'PY'
import os,base64
print(base64.urlsafe_b64encode(os.urandom(18)).decode().rstrip("="))
PY
)"
CREATE_BODY="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" python3 - <<'PY'
import json, os
body={
  "merchant_id": os.environ["MERCHANT_ID"],
  "platform": (os.environ["PLATFORM"] or "").strip().lower(),
  "platform_product_id": os.environ["PLATFORM_PRODUCT_ID"],
  "rating": 5,
  "title": "Great product",
  "body": "Works as expected.",
}
vid=(os.environ.get("VARIANT_ID") or "").strip()
if vid:
  body["variant_id"]=vid
print(json.dumps(body))
PY
)"
CREATE_RESP="$(curl -sS -H "Content-Type: application/json" -H "Authorization: Bearer $SUBMISSION_TOKEN" -H "Idempotency-Key: $IDEMPOTENCY_KEY" -d "$CREATE_BODY" "$CREATE_URL")"
REVIEW_ID="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("review_id",""))' 2>/dev/null || true)"
STATE="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("moderation_state",""))' 2>/dev/null || true)"
echo "review_id=$REVIEW_ID moderation_state=$STATE"
[[ -n "$REVIEW_ID" ]] || { echo "ERROR: no review_id" >&2; echo "$CREATE_RESP" >&2; exit 1; }

echo "== upload media (still not visible until approved) =="
UPLOAD_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" \
  -F "file=@$MEDIA_FILE_PATH" \
  "$REVIEWS_BASE_URL/buyer/reviews/v1/reviews/$REVIEW_ID/media")"
MEDIA_PUBLIC_ID="$(printf '%s' "$UPLOAD_RESP" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("media") or {}).get("public_id",""))' 2>/dev/null || true)"
echo "upload_ok public_id=$MEDIA_PUBLIC_ID"
[[ -n "$MEDIA_PUBLIC_ID" ]] || { echo "ERROR: missing public_id from upload" >&2; echo "$UPLOAD_RESP" >&2; exit 1; }

echo "== pre-check visible on read path (should be not_found) =="
PRE="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":\"$VARIANT_ID\"},\"filters\":{\"limit\":50}}}" \
  "$REVIEWS_BASE_URL/agent/shop/v1/invoke" \
| python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; hit=[it for it in items if int(it.get("review_id") or -1)==rid]; print("found" if hit else "not_found")' "$REVIEW_ID")"
echo "$PRE"

echo "== approve -> active (employee) =="
EMPLOYEE_ID="emp_001"
EMAIL="employee+smoke@pivota.invalid"
JWT_SECRET_KEY="${EMPLOYEE_JWT_SECRET_KEY:-${JWT_SECRET_KEY:-}}"
if [[ -z "$JWT_SECRET_KEY" ]]; then
  read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " JWT_SECRET_KEY
  echo
fi
[[ -n "$JWT_SECRET_KEY" ]] || { echo "ERROR: empty JWT_SECRET_KEY" >&2; exit 1; }
EMP_TOKEN="$(JWT_SECRET_KEY="$JWT_SECRET_KEY" EMPLOYEE_ID="$EMPLOYEE_ID" EMAIL="$EMAIL" python3 -c 'import os,base64,hmac,hashlib,json,time
def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
now=int(time.time())
hdr={"alg":"HS256","typ":"JWT"}
pl={"sub":os.environ["EMPLOYEE_ID"],"user_id":os.environ["EMPLOYEE_ID"],"employee_id":os.environ["EMPLOYEE_ID"],
    "email":os.environ["EMAIL"],"role":"admin","permissions":["reviews.moderate.status"],
    "iat":now,"exp":now+3600}
s=b64u(json.dumps(hdr,separators=(",",":")).encode())+"."+b64u(json.dumps(pl,separators=(",",":")).encode())
sig=b64u(hmac.new(os.environ["JWT_SECRET_KEY"].encode(), s.encode(), hashlib.sha256).digest())
print(s+"."+sig)')"
unset JWT_SECRET_KEY
curl -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"buyer smoke approve"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
echo "approved_ok"

echo "== post-check visible on read path (should be found) =="
POST="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":\"$VARIANT_ID\"},\"filters\":{\"limit\":50}}}" \
  "$REVIEWS_BASE_URL/agent/shop/v1/invoke" \
| python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; hit=[it for it in items if int(it.get("review_id") or -1)==rid]; print("found" if hit else "not_found")' "$REVIEW_ID")"
echo "$POST"

echo "== fetch signed media URL via read path =="
SIGNED_PATH="$(curl -sS -H "Content-Type: application/json" \
  -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":\"$VARIANT_ID\"},\"filters\":{\"limit\":50}}}" \
  "$REVIEWS_BASE_URL/agent/shop/v1/invoke" \
| python3 -c 'import sys,json; o=json.load(sys.stdin); pid=sys.argv[1]; items=o.get("items") or []; urls=[(m.get("url") or "") for it in items for m in (it.get("media") or []) if pid in (m.get("url") or "")]; print(urls[0] if urls else "")' "$MEDIA_PUBLIC_ID")"
[[ -n "$SIGNED_PATH" ]] || { echo "ERROR: no signed media url found in list_sku_reviews" >&2; exit 1; }
SIGNED_URL="$REVIEWS_BASE_URL$SIGNED_PATH"
echo "SIGNED_URL=$SIGNED_URL"

echo "== 200/304 smoke =="
IP1="9.9.9.$((RANDOM%200+1))"
IP2="9.9.8.$((RANDOM%200+1))"
HDR="$(mktemp "${TMPDIR:-/tmp}/buyer_media_headers.XXXXXX")"
curl -sS -D "$HDR" -o /dev/null -H "X-Forwarded-For: $IP1" "$SIGNED_URL"
ETAG="$(awk -F': ' 'tolower($1)=="etag"{print $2}' "$HDR" | tr -d '\r')"
CODE="$(awk 'NR==1{print $2}' "$HDR" | tr -d '\r')"
rm -f "$HDR" || true
echo "status=$CODE etag=$ETAG"
if [[ -n "$ETAG" ]]; then
  curl -sS -o /dev/null -w "status_304=%{http_code}\n" -H "X-Forwarded-For: $IP2" -H "If-None-Match: $ETAG" "$SIGNED_URL"
fi

echo "== signature negative tests =="
curl -sS -o /dev/null -w "missing_sig_status=%{http_code}\n" "$REVIEWS_BASE_URL/agent/shop/v1/review-media/$MEDIA_PUBLIC_ID"
TAMPER_URL="$(printf '%s' "$SIGNED_URL" | python3 -c 'import sys,urllib.parse; u=sys.stdin.read().strip(); p=urllib.parse.urlsplit(u); q=urllib.parse.parse_qs(p.query); q["exp"]=[str(int(q.get("exp",[0])[0])+1)]; nq=urllib.parse.urlencode({k:v[0] for k,v in q.items()}); print(urllib.parse.urlunsplit((p.scheme,p.netloc,p.path,nq,p.fragment)))')"
curl -sS -o /dev/null -w "tamper_exp_status=%{http_code}\n" "$TAMPER_URL"

echo "OK"
