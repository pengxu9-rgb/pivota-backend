#!/bin/bash
set -euo pipefail

# Smoke: create buyer review (under_review) -> verify it appears in moderation queue -> approve -> verify it disappears.
#
# Required env:
# - REVIEWS_BASE_URL
# - PROOF_ISSUER_BASE_URL
# - MERCHANT_ID
# - PLATFORM_PRODUCT_ID
#
# Optional env:
# - PLATFORM (default: shopify)
# - VARIANT_ID
# - MEDIA_FILE_PATH or MOCK_MEDIA_DIR
# - PROOF_ISSUER_INTERNAL_KEY (otherwise prompts)
# - EMPLOYEE_JWT_SECRET_KEY or JWT_SECRET_KEY (otherwise prompts)

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
MEDIA_FILE_PATH="${MEDIA_FILE_PATH:-}"

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }

if [[ -z "$REVIEWS_BASE_URL" ]]; then echo "ERROR: missing REVIEWS_BASE_URL" >&2; exit 1; fi
if [[ -z "$PROOF_ISSUER_BASE_URL" ]]; then echo "ERROR: missing PROOF_ISSUER_BASE_URL" >&2; exit 1; fi
if [[ -z "$MERCHANT_ID" ]]; then echo "ERROR: missing MERCHANT_ID" >&2; exit 1; fi
if [[ -z "$PLATFORM_PRODUCT_ID" ]]; then echo "ERROR: missing PLATFORM_PRODUCT_ID" >&2; exit 1; fi

_pick_media_file() {
  local default_dir="$HOME/Desktop/mock_review_media"
  local candidates_dir="${MOCK_MEDIA_DIR:-$default_dir}"
  if [[ -n "${MEDIA_FILE_PATH:-}" ]]; then
    printf '%s\n' "$MEDIA_FILE_PATH"
    return 0
  fi
  if [[ ! -d "$candidates_dir" ]]; then
    echo "ERROR: MEDIA_FILE_PATH not set and MOCK_MEDIA_DIR not found: $candidates_dir" >&2
    return 1
  fi
  find "$candidates_dir" -maxdepth 2 -type f \( -iname '*.jpg' -o -iname '*.jpeg' -o -iname '*.png' -o -iname '*.webp' \) 2>/dev/null | head -n 1
}

MEDIA_FILE_PATH="$(_pick_media_file || true)"
if [[ -z "$MEDIA_FILE_PATH" || ! -f "$MEDIA_FILE_PATH" ]]; then
  echo "ERROR: MEDIA_FILE_PATH not found: $MEDIA_FILE_PATH" >&2
  exit 1
fi
echo "media_file=$MEDIA_FILE_PATH"

INTERNAL_KEY="${PROOF_ISSUER_INTERNAL_KEY:-}"
if [[ -z "$INTERNAL_KEY" ]]; then
  read -r -s -p "X-Internal-Key (proof issuer): " INTERNAL_KEY
  echo
fi
[[ -n "$INTERNAL_KEY" ]] || { echo "ERROR: empty internal key" >&2; exit 1; }

JWT_SECRET_KEY="${EMPLOYEE_JWT_SECRET_KEY:-${JWT_SECRET_KEY:-}}"
if [[ -z "$JWT_SECRET_KEY" ]]; then
  read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " JWT_SECRET_KEY
  echo
fi
[[ -n "$JWT_SECRET_KEY" ]] || { echo "ERROR: empty JWT_SECRET_KEY" >&2; exit 1; }

EMP_TOKEN="$(JWT_SECRET_KEY="$JWT_SECRET_KEY" python3 -c 'import os,base64,hmac,hashlib,json,time
def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
now=int(time.time())
hdr={"alg":"HS256","typ":"JWT"}
pl={"sub":"emp_ops","user_id":"emp_ops","employee_id":"emp_ops","email":"ops+reviews@pivota.invalid","role":"admin",
    "permissions":["reviews.read","reviews.moderate.list","reviews.moderate.status"],"iat":now,"exp":now+3600}
msg=b64u(json.dumps(hdr,separators=(",",":")).encode())+"."+b64u(json.dumps(pl,separators=(",",":")).encode())
sig=b64u(hmac.new(os.environ["JWT_SECRET_KEY"].encode(), msg.encode(), hashlib.sha256).digest())
print(msg+"."+sig)')"
unset JWT_SECRET_KEY

ISSUE_URL="$PROOF_ISSUER_BASE_URL/internal/reviews/v1/proof/issue"
EXCHANGE_URL="$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange"
CREATE_URL="$REVIEWS_BASE_URL/buyer/reviews/v1/reviews"

SUBJECT_JSON="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" python3 - <<'PY'
import json, os
mid=os.environ["MERCHANT_ID"]
platform=os.environ["PLATFORM"]
pp=os.environ["PLATFORM_PRODUCT_ID"]
vid=(os.environ.get("VARIANT_ID") or "").strip()
subject={"merchant_id":mid,"platform":platform,"platform_product_id":pp}
if vid:
  subject["variant_id"]=vid
print(json.dumps({"merchant_id":mid,"subjects":[subject],"verification":"unverified","ttl_seconds":600}))
PY
)"

echo "== issue proof token =="
ISSUE_RESP="$(curl -sS -H "Content-Type: application/json" -H "X-Internal-Key: $INTERNAL_KEY" -d "$SUBJECT_JSON" "$ISSUE_URL")"
PROOF_TOKEN="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proof_token",""))')"
[[ -n "$PROOF_TOKEN" ]] || { echo "ERROR: no proof_token" >&2; echo "$ISSUE_RESP" >&2; exit 1; }
echo "issue_ok"

echo "== exchange proof -> submission_token =="
EXCHANGE_RESP="$(curl -sS -H "Content-Type: application/json" -H "Authorization: Bearer $PROOF_TOKEN" -d '{"ttl_seconds":900}' "$EXCHANGE_URL")"
SUBMISSION_TOKEN="$(printf '%s' "$EXCHANGE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("submission_token",""))')"
[[ -n "$SUBMISSION_TOKEN" ]] || { echo "ERROR: no submission_token" >&2; echo "$EXCHANGE_RESP" >&2; exit 1; }
echo "exchange_ok"

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
  "platform": os.environ["PLATFORM"],
  "platform_product_id": os.environ["PLATFORM_PRODUCT_ID"],
  "rating": 5,
  "title": "Moderation queue smoke",
  "body": "Queue test."
}
vid=(os.environ.get("VARIANT_ID") or "").strip()
if vid:
  body["variant_id"]=vid
print(json.dumps(body))
PY
)"
CREATE_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -H "Content-Type: application/json" -H "Idempotency-Key: $IDEMPOTENCY_KEY" -d "$CREATE_BODY" "$CREATE_URL")"
REVIEW_ID="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; o=json.load(sys.stdin); print(str((o.get("review") or {}).get("id") or o.get("review_id") or ""))' 2>/dev/null || true)"
[[ -n "$REVIEW_ID" ]] || { echo "ERROR: missing review_id" >&2; echo "$CREATE_RESP" >&2; exit 1; }
echo "review_id=$REVIEW_ID"

echo "== upload media (still under_review) =="
UPLOAD_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -F "file=@${MEDIA_FILE_PATH}" "$REVIEWS_BASE_URL/buyer/reviews/v1/reviews/$REVIEW_ID/media")"
MEDIA_PUBLIC_ID="$(printf '%s' "$UPLOAD_RESP" | python3 -c 'import sys,json; print((json.load(sys.stdin).get("media") or {}).get("public_id",""))' 2>/dev/null || true)"
echo "upload_ok public_id=$MEDIA_PUBLIC_ID"

echo "== moderation queue list (expect found) =="
LIST_RESP="$(curl --http1.1 -sS -H "Authorization: Bearer $EMP_TOKEN" \
  "$REVIEWS_BASE_URL/employee/reviews/v1/moderation/reviews?limit=50&status=under_review&merchant_id=$MERCHANT_ID&source_system=buyer")"
FOUND="$(printf '%s' "$LIST_RESP" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; print("found" if any(int(it.get("id") or -1)==rid for it in items) else "not_found")' "$REVIEW_ID")"
echo "$FOUND"
[[ "$FOUND" == "found" ]] || { echo "ERROR: review not found in moderation queue" >&2; exit 1; }

echo "== approve -> active (employee) =="
curl -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"moderation queue smoke approve"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
echo "approved_ok"

echo "== moderation queue list (expect not_found) =="
LIST_RESP2="$(curl --http1.1 -sS -H "Authorization: Bearer $EMP_TOKEN" \
  "$REVIEWS_BASE_URL/employee/reviews/v1/moderation/reviews?limit=50&status=under_review&merchant_id=$MERCHANT_ID&source_system=buyer")"
FOUND2="$(printf '%s' "$LIST_RESP2" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; print("found" if any(int(it.get("id") or -1)==rid for it in items) else "not_found")' "$REVIEW_ID")"
echo "$FOUND2"
[[ "$FOUND2" == "not_found" ]] || { echo "ERROR: review still in under_review after approval" >&2; exit 1; }

echo "OK"
