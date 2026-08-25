#!/bin/bash
set -euo pipefail

# Ops helper: inspect Reviews moderation queue quickly.
#
# Usage:
#   /bin/bash scripts/ops_reviews_moderation_queue.sh
#
# Env (optional):
# - BASE_URL (default: https://api.pivota.cc)
# - MERCHANT_ID (optional filter)
# - LIMIT (default: 50)
# - SET_REVIEW_ID (optional; if set, call employee status update)
# - SET_STATUS (default: active; used with SET_REVIEW_ID)
# - SET_REASON (default: "ops update"; used with SET_REVIEW_ID)
#
# Prompts (no echo):
# - JWT_SECRET_KEY (to mint an employee JWT)

BASE_URL="${BASE_URL:-https://api.pivota.cc}"
MERCHANT_ID="${MERCHANT_ID:-}"
LIMIT="${LIMIT:-50}"
SET_REVIEW_ID="${SET_REVIEW_ID:-}"
SET_STATUS="${SET_STATUS:-active}"
SET_REASON="${SET_REASON:-ops update}"

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }

read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " JWT_SECRET_KEY
echo

EMP_TOKEN="$(
  JWT_SECRET_KEY="$JWT_SECRET_KEY" python3 -c 'import os,base64,hmac,hashlib,json,time
def b64u(b): return base64.urlsafe_b64encode(b).decode().rstrip("=")
now=int(time.time())
hdr={"alg":"HS256","typ":"JWT"}
pl={"sub":"emp_ops","user_id":"emp_ops","employee_id":"emp_ops","email":"ops+reviews@pivota.invalid","role":"admin",
    "permissions":["reviews.read","reviews.moderate.list","reviews.moderate.status"],"iat":now,"exp":now+3600}
msg=b64u(json.dumps(hdr,separators=(",",":")).encode())+"."+b64u(json.dumps(pl,separators=(",",":")).encode())
sig=b64u(hmac.new(os.environ["JWT_SECRET_KEY"].encode(), msg.encode(), hashlib.sha256).digest())
print(msg+"."+sig)'
)"
unset JWT_SECRET_KEY

_set_status() {
  local review_id="$1"
  local status="$2"
  local reason="$3"

  if ! printf '%s' "$review_id" | python3 -c 'import sys; s=sys.stdin.read().strip(); sys.exit(0 if s.isdigit() else 1)'; then
    echo "ERROR: SET_REVIEW_ID must be numeric (got: $review_id)" >&2
    return 2
  fi

  local url="$BASE_URL/employee/reviews/v1/reviews/$review_id/status"
  local tmp_body tmp_hdr http
  tmp_body="$(mktemp "${TMPDIR:-/tmp}/reviews_set_status_body.XXXXXX")"
  tmp_hdr="$(mktemp "${TMPDIR:-/tmp}/reviews_set_status_hdr.XXXXXX")"
  http="$(curl --http1.1 -sS -D "$tmp_hdr" -o "$tmp_body" -w "%{http_code}" \
    -H "Authorization: Bearer $EMP_TOKEN" -H "Content-Type: application/json" \
    -d "{\"status\":\"$status\",\"reason\":\"$reason\"}" \
    "$url" || true)"

  if [[ "$http" != "200" ]]; then
    echo "ERROR: set_status http_status=$http url=$url" >&2
    head -n 20 "$tmp_hdr" >&2 || true
    if [[ -s "$tmp_body" ]] && head -c 1 "$tmp_body" | grep -q '{'; then
      echo "--- body (first 1024B) ---" >&2
      head -c 1024 "$tmp_body" >&2 || true
    fi
    rm -f "$tmp_body" "$tmp_hdr" || true
    return 2
  fi

  if python3 -c 'import sys,json; json.load(sys.stdin)' <"$tmp_body" >/dev/null 2>&1; then
    echo "set_status_ok review_id=$review_id new_status=$status"
  else
    echo "set_status_ok review_id=$review_id new_status=$status (non-json body)"
  fi
  rm -f "$tmp_body" "$tmp_hdr" || true
}

_q() {
  local status="$1"
  local source_system="$2"
  local url="$BASE_URL/employee/reviews/v1/moderation/reviews?limit=$LIMIT&status=$status&source_system=$source_system"
  if [[ -n "$MERCHANT_ID" ]]; then
    url="$url&merchant_id=$MERCHANT_ID"
  fi
  if [[ -z "${EMP_TOKEN:-}" || "${#EMP_TOKEN}" -lt 50 ]]; then
    echo "ERROR: employee token missing/too short" >&2
    return 2
  fi

  local tmp_body tmp_hdr http
  tmp_body="$(mktemp "${TMPDIR:-/tmp}/reviews_moderation_body.XXXXXX")"
  tmp_hdr="$(mktemp "${TMPDIR:-/tmp}/reviews_moderation_hdr.XXXXXX")"
  http="$(curl --http1.1 -sS -D "$tmp_hdr" -o "$tmp_body" -w "%{http_code}" -H "Authorization: Bearer $EMP_TOKEN" "$url" || true)"
  if [[ -z "$http" ]]; then
    echo "ERROR: curl failed (no http_code)" >&2
    head -n 20 "$tmp_hdr" >&2 2>/dev/null || true
    rm -f "$tmp_body" "$tmp_hdr" || true
    return 2
  fi

  if [[ "$http" != "200" ]]; then
    echo "ERROR: http_status=$http url=$url" >&2
    echo "--- headers ---" >&2
    head -n 20 "$tmp_hdr" >&2 || true
    # Avoid printing potential PII from review bodies; only print JSON error-ish responses.
    if [[ -s "$tmp_body" ]] && head -c 1 "$tmp_body" | grep -q '{'; then
      echo "--- body (first 1024B) ---" >&2
      head -c 1024 "$tmp_body" >&2 || true
    fi
    rm -f "$tmp_body" "$tmp_hdr" || true
    return 2
  fi

  if [[ ! -s "$tmp_body" ]]; then
    echo "ERROR: empty body (http_status=200) url=$url" >&2
    rm -f "$tmp_body" "$tmp_hdr" || true
    return 2
  fi

  if ! python3 -c 'import sys,json; json.load(sys.stdin)' <"$tmp_body" >/dev/null 2>&1; then
    echo "ERROR: response is not JSON (http_status=200) url=$url" >&2
    # For 200 but non-JSON, it's typically an HTML/proxy response; safe to show a small prefix.
    echo "--- body (first 256B) ---" >&2
    head -c 256 "$tmp_body" >&2 || true
    rm -f "$tmp_body" "$tmp_hdr" || true
    return 2
  fi

  cat "$tmp_body"
  rm -f "$tmp_body" "$tmp_hdr" || true
}

_summarize() {
  python3 -c 'import sys,json
o=json.load(sys.stdin)
items=o.get("items") or []
print("count=%d limit=%s" % (len(items), o.get("limit")))
for it in items[:10]:
  print("- id=%s merchant_id=%s product_id=%s variant_id=%s media_count=%s status=%s source_system=%s" % (
    it.get("id"), it.get("merchant_id"), it.get("platform_product_id"), it.get("variant_id"),
    it.get("media_count"), it.get("status"), it.get("source_system"),
  ))'
}

_summarize_has_media() {
  python3 -c 'import sys,json
o=json.load(sys.stdin)
items=[it for it in (o.get("items") or []) if int(it.get("media_count") or 0) > 0]
print("count=%d" % len(items))
for it in items[:10]:
  print("- id=%s merchant_id=%s product_id=%s variant_id=%s media_count=%s status=%s source_system=%s" % (
    it.get("id"), it.get("merchant_id"), it.get("platform_product_id"), it.get("variant_id"),
    it.get("media_count"), it.get("status"), it.get("source_system"),
  ))'
}

echo "== under_review (buyer) =="
UNDER_REVIEW_JSON="$(_q under_review buyer)"
printf '%s' "$UNDER_REVIEW_JSON" | _summarize
echo

echo "== under_review (buyer, has_media) =="
printf '%s' "$UNDER_REVIEW_JSON" | _summarize_has_media
echo

echo "== removed (buyer) =="
REMOVED_JSON="$(_q removed buyer)"
printf '%s' "$REMOVED_JSON" | _summarize

if [[ -n "$SET_REVIEW_ID" ]]; then
  echo
  echo "== set status =="
  _set_status "$SET_REVIEW_ID" "$SET_STATUS" "$SET_REASON"
fi

echo "OK"
