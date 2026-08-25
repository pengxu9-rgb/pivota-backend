#!/bin/bash
set -euo pipefail

# E2E smoke: proof-issuer -> exchange -> buyer create -> buyer media -> employee approve -> read path visible + media 200/304
#
# Required env:
# - PROOF_ISSUER_BASE_URL  NO REACHABLE PRODUCTION VALUE - see below.
# - REVIEWS_BASE_URL      (prod: https://api.pivota.cc)
#
#   Production is Cloud Run in pivota-prod/us-west1. `api.pivota.cc` is public and
#   is the right REVIEWS_BASE_URL. The proof-issuer is NOT: the `proof-issuer`
#   service runs with `ingress: internal-and-cloud-load-balancing`, its bare
#   *.run.app URL 404s from outside, and `pivota-urlmap` publishes no hostname for
#   it (verified 2026-08-25). So there is deliberately no production value written
#   here - naming the run.app URL would ship a command that always fails. Run the
#   proof-issuer leg from inside the VPC, or point it at a local/staging issuer.
#
#   Do not fall back to a *.up.railway.app host. Railway is RETIRED (#1872), and its
#   liveness is perishable in both directions: the production agent host answered
#   200 on the morning of 2026-08-25 and by that afternoon returned 404 with
#   `x-railway-fallback: true` (no service bound). A smoke that passes there
#   passed against a platform nobody is served from; one that fails there tells
#   you nothing about production.
# - MERCHANT_ID
# - PLATFORM_PRODUCT_ID
#
# Optional env:
# - PLATFORM (default: shopify)
# - VARIANT_ID
# - MEDIA_FILE_PATH or MOCK_MEDIA_DIR (default: ~/Desktop/mock_review_media)
# - PROOF_ISSUER_INTERNAL_KEY (otherwise prompts)
# - EMPLOYEE_JWT_SECRET_KEY or JWT_SECRET_KEY (otherwise prompts)
# - RUN_REMOVE_TEST=true|false (default: false) remove review after approval and verify read path + media deny
# - WAIT_FOR_REDEPLOY=true|false (default: false) pause for redeploy then verify the same media public_id still readable (requires persistent storage)
# - AUTO_APPROVE=true|false (default: true) stop before employee approval (useful for moderation queue testing)

PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
MEDIA_FILE_PATH="${MEDIA_FILE_PATH:-}"
RUN_REMOVE_TEST="${RUN_REMOVE_TEST:-false}"
WAIT_FOR_REDEPLOY="${WAIT_FOR_REDEPLOY:-false}"
AUTO_APPROVE="${AUTO_APPROVE:-true}"

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
unset INTERNAL_KEY

PROOF_TOKEN="$(printf '%s' "$ISSUE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("proof_token",""))')"
if [[ -z "$PROOF_TOKEN" ]]; then
  echo "ERROR: no proof_token from issuer" >&2
  if printf '%s' "$ISSUE_RESP" | grep -q 'UNAUTHORIZED'; then
    echo "HINT: wrong proof-issuer internal key. Use the key from the proof-issuer service (not the web backend invitation key)." >&2
    echo "      You can set PROOF_ISSUER_INTERNAL_KEY (or REVIEWS_PROOF_ISSUER_INTERNAL_KEY)." >&2
  fi
  echo "$ISSUE_RESP" >&2
  exit 1
fi
echo "issue_ok"

echo "== exchange proof -> submission_token =="
EXCHANGE_RESP="$(curl -sS -H "Content-Type: application/json" -H "Authorization: Bearer $PROOF_TOKEN" -d '{"ttl_seconds":900}' "$EXCHANGE_URL")"
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
  "platform": os.environ["PLATFORM"],
  "platform_product_id": os.environ["PLATFORM_PRODUCT_ID"],
  "rating": 5,
  "title": "Buyer smoke",
  "body": "Buyer submission smoke."
}
vid=(os.environ.get("VARIANT_ID") or "").strip()
if vid:
  body["variant_id"]=vid
print(json.dumps(body))
PY
)"
CREATE_RESP="$(curl -sS -H "Content-Type: application/json" \
  -H "Authorization: Bearer $SUBMISSION_TOKEN" \
  -H "Idempotency-Key: $IDEMPOTENCY_KEY" \
  -d "$CREATE_BODY" "$CREATE_URL")"
REVIEW_ID="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("review_id",""))')"
STATE="$(printf '%s' "$CREATE_RESP" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("moderation_state",""))')"
echo "review_id=$REVIEW_ID moderation_state=$STATE"
[[ -n "$REVIEW_ID" ]] || { echo "ERROR: missing review_id" >&2; echo "$CREATE_RESP" >&2; exit 1; }

echo "== upload media (still not visible until approved) =="
UPLOAD_RESP="$(curl -sS -H "Authorization: Bearer $SUBMISSION_TOKEN" -F "file=@${MEDIA_FILE_PATH}" \
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

if [[ "$AUTO_APPROVE" != "true" ]]; then
  echo "AUTO_APPROVE=false stop_before_approval=1 review_id=$REVIEW_ID media_public_id=$MEDIA_PUBLIC_ID"
  exit 0
fi

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

APPROVE_HDR="$(mktemp "${TMPDIR:-/tmp}/approve_hdr.XXXXXX")"
APPROVE_BODY="$(mktemp "${TMPDIR:-/tmp}/approve_body.XXXXXX")"
APPROVE_CODE="$(curl -sS -D "$APPROVE_HDR" -o "$APPROVE_BODY" -w '%{http_code}' \
  -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
  -d '{"status":"active","reason":"buyer smoke approve"}' \
  "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" || true)"

REQ_ID="$(awk -F': ' 'tolower($1)=="x-request-id"{print $2}' "$APPROVE_HDR" | tr -d '\r' | head -n 1)"
if [[ "$APPROVE_CODE" == "200" ]]; then
  if python3 - <<'PY' <"$APPROVE_BODY" >/dev/null
import json,sys
o=json.load(sys.stdin)
assert o.get("status")=="success"
PY
  then
    echo "approve_api_ok=1"
  else
    echo "approve_api_ok=0"
    echo "ERROR: approval returned http=200 but status!=success (x-request-id=${REQ_ID:-})" >&2
    head -c 1200 "$APPROVE_BODY" >&2 || true
    echo >&2
    rm -f "$APPROVE_HDR" "$APPROVE_BODY" || true
    exit 1
  fi
else
  echo "approve_api_ok=0"
  echo "ERROR: approval failed http=$APPROVE_CODE (x-request-id=${REQ_ID:-})" >&2
  head -c 1200 "$APPROVE_BODY" >&2 || true
  echo >&2
  rm -f "$APPROVE_HDR" "$APPROVE_BODY" || true
  exit 1
fi
rm -f "$APPROVE_HDR" "$APPROVE_BODY" || true
echo "approved_ok"

_list_reviews_json() {
  local variant="${1:-}"
  local variant_json
  if [[ -n "$variant" ]]; then
    variant_json="$(python3 -c 'import json,sys; print(json.dumps(sys.stdin.read().strip()))' <<<"$variant")"
    curl -sS -H "Content-Type: application/json" \
      -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":$variant_json},\"filters\":{\"limit\":50}}}" \
      "$REVIEWS_BASE_URL/agent/shop/v1/invoke"
  else
    curl -sS -H "Content-Type: application/json" \
      -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\"},\"filters\":{\"limit\":50}}}" \
      "$REVIEWS_BASE_URL/agent/shop/v1/invoke"
  fi
}

_poll_until_visible() {
  local max_tries="${1:-20}"
  local sleep_s="${2:-1}"
  local try=1
  local last_json=""
  while [[ $try -le $max_tries ]]; do
    last_json="$(_list_reviews_json "$VARIANT_ID" || true)"
    if [[ -n "$last_json" ]]; then
      if printf '%s' "$last_json" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; print("found" if any(int(it.get("review_id") or -1)==rid for it in items) else "not_found")' "$REVIEW_ID" | grep -q '^found$'; then
        printf '%s' "$last_json"
        return 0
      fi
    fi
    sleep "$sleep_s" || true
    try=$((try+1))
  done
  printf '%s' "$last_json"
  return 1
}

echo "== post-check visible on read path (should be found) =="
POST_JSON="$(_poll_until_visible 25 1)" || true
POST_STATE="$(printf '%s' "$POST_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; print("found" if any(int(it.get("review_id") or -1)==rid for it in items) else "not_found")' "$REVIEW_ID" 2>/dev/null || echo "not_found")"
echo "$POST_STATE"
if [[ "$POST_STATE" != "found" ]]; then
  echo "ERROR: approved review not visible on read path after polling" >&2
  echo "HINT: possible cache/index delay or approval failure; dumping last list_sku_reviews response (truncated)..." >&2
  printf '%s' "$POST_JSON" | head -c 800 >&2 || true
  echo >&2
  exit 1
fi

echo "== fetch signed media URL via read path =="
SIGNED_PATH="$(printf '%s' "$POST_JSON" | python3 -c 'import sys,json; o=json.load(sys.stdin); pid=sys.argv[1]; items=o.get("items") or []; urls=[(m.get("url") or "") for it in items for m in (it.get("media") or []) if pid in (m.get("url") or "")]; print(urls[0] if urls else "")' "$MEDIA_PUBLIC_ID" 2>/dev/null || true)"
[[ -n "$SIGNED_PATH" ]] || { echo "ERROR: no signed media url found in list_sku_reviews (media may still be pending activation)" >&2; exit 1; }
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

if [[ "$WAIT_FOR_REDEPLOY" == "true" ]]; then
  echo "== redeploy persistence check =="
  echo "NOW redeploy the reviews backend, then press Enter."
  echo "  Production is Cloud Run (pivota-prod/us-west1). Do not deploy by hand -"
  echo "  a hand 'gcloud run deploy' skips the candidate/health-gate/tag-sweep flow:"
  echo "    CONFIG=preserve infra/gcp/deploy_backend.sh prod <sha>"
  echo "  This check only needs new instances, so an env-var no-op also works:"
  echo "    gcloud run services update web --project pivota-prod --region us-west1 \\"
  echo "      --update-env-vars REDEPLOY_MARKER=\$(date +%s)"
  echo "  Confirm by the SERVING REVISION NAME, not /version - an env-only update"
  echo "  reuses the image, so the build SHA is identical either way:"
  echo "    gcloud run services describe web --project pivota-prod --region us-west1 \\"
  echo "      --format='value(status.latestReadyRevisionName)'"
  read -r _

  echo "== find NEW signed url for same public_id (after redeploy) =="
  SIGNED_PATH2="$(curl -sS -H "Content-Type: application/json" \
    -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":\"$VARIANT_ID\"},\"filters\":{\"limit\":50}}}" \
    "$REVIEWS_BASE_URL/agent/shop/v1/invoke" \
  | python3 -c 'import sys,json; o=json.load(sys.stdin); pid=sys.argv[1]; items=o.get("items") or []; urls=[(m.get("url") or "") for it in items for m in (it.get("media") or []) if pid in (m.get("url") or "")]; print(urls[0] if urls else "")' "$MEDIA_PUBLIC_ID")"
  [[ -n "$SIGNED_PATH2" ]] || { echo "ERROR: no signed media url found after redeploy for public_id=$MEDIA_PUBLIC_ID" >&2; exit 1; }
  SIGNED_URL2="$REVIEWS_BASE_URL$SIGNED_PATH2"
  echo "SIGNED_URL2=$SIGNED_URL2"

  echo "== fetch after redeploy (expect 200) =="
  IP4="9.9.6.$((RANDOM%200+1))"
  CODE2="$(curl -sS -o /dev/null -w "%{http_code}" -H "X-Forwarded-For: $IP4" -H "Cache-Control: no-cache" "$SIGNED_URL2")"
  echo "status=$CODE2"
  [[ "$CODE2" == "200" || "$CODE2" == "304" ]] || { echo "ERROR: media not readable after redeploy (status=$CODE2)" >&2; exit 1; }
fi

if [[ "$RUN_REMOVE_TEST" == "true" ]]; then
  echo "== set removed (reason required) =="
  curl -sS -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
    -d '{"status":"removed","reason":"buyer smoke remove"}' \
    "$REVIEWS_BASE_URL/employee/reviews/v1/reviews/$REVIEW_ID/status" >/dev/null
  echo "removed_ok"

  echo "== post-remove visible on read path? (expect not_found) =="
  POST_REMOVE="$(curl -sS -H "Content-Type: application/json" \
    -d "{\"operation\":\"list_sku_reviews\",\"payload\":{\"sku\":{\"merchant_id\":\"$MERCHANT_ID\",\"platform\":\"$PLATFORM\",\"platform_product_id\":\"$PLATFORM_PRODUCT_ID\",\"variant_id\":\"$VARIANT_ID\"},\"filters\":{\"limit\":50}}}" \
    "$REVIEWS_BASE_URL/agent/shop/v1/invoke" \
  | python3 -c 'import sys,json; o=json.load(sys.stdin); rid=int(sys.argv[1]); items=o.get("items") or []; hit=[it for it in items if int(it.get("review_id") or -1)==rid]; print("found" if hit else "not_found")' "$REVIEW_ID")"
  echo "$POST_REMOVE"

  echo "== post-remove signed media should NOT be readable (expect non-200; cache bypass) =="
  IP3="9.9.7.$((RANDOM%200+1))"
  CODE_REMOVED="$(curl -sS -o /dev/null -w "%{http_code}" -H "X-Forwarded-For: $IP3" -H "Cache-Control: no-cache" "$SIGNED_URL")"
  echo "status=$CODE_REMOVED"
  if [[ "$CODE_REMOVED" == "200" || "$CODE_REMOVED" == "304" ]]; then
    echo "ERROR: media still readable after removed (status=$CODE_REMOVED)" >&2
    exit 1
  fi
fi

echo "OK"
