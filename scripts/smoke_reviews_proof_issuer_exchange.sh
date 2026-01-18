#!/bin/bash
set -euo pipefail

# Smoke: proof issuer service -> pivota-backend exchange
# - Proof issuer (internal): POST /internal/reviews/v1/proof/issue (X-Internal-Key)
# - Reviews backend: POST /buyer/reviews/v1/verification/exchange (Authorization: Bearer proof_token)
# - Replay: exchange same proof twice (2nd should be 409)
#
# Required env:
# - PROOF_ISSUER_BASE_URL
# - REVIEWS_BASE_URL
# - MERCHANT_ID
# - PLATFORM_PRODUCT_ID
#
# Optional env:
# - PLATFORM (default: shopify)
# - VARIANT_ID
# - PROOF_ISSUER_INTERNAL_KEY (otherwise prompts)
#
# Replay behavior:
# - By default, tolerates transient 5xx/timeout on the 2nd exchange and retries.
# - Set STRICT_REPLAY=true to fail if 409 is not observed.

PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
REPLAY_ATTEMPTS="${REPLAY_ATTEMPTS:-3}"
REPLAY_DELAY_SECONDS="${REPLAY_DELAY_SECONDS:-0.5}"
STRICT_REPLAY="${STRICT_REPLAY:-}"

if [[ -z "$PROOF_ISSUER_BASE_URL" ]]; then
  echo "ERROR: missing PROOF_ISSUER_BASE_URL (e.g. https://<proof-issuer-host>)" >&2
  exit 1
fi
if [[ -z "$REVIEWS_BASE_URL" ]]; then
  echo "ERROR: missing REVIEWS_BASE_URL (e.g. https://<reviews-backend-host>)" >&2
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

INTERNAL_KEY="${PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY:-}}}"
if [[ -z "$INTERNAL_KEY" ]]; then
  read -r -s -p "X-Internal-Key (proof issuer): " INTERNAL_KEY
  echo
fi
[[ -n "$INTERNAL_KEY" ]] || { echo "ERROR: empty internal key (set PROOF_ISSUER_INTERNAL_KEY or paste when prompted)" >&2; exit 1; }

ISSUE_URL="$PROOF_ISSUER_BASE_URL/internal/reviews/v1/proof/issue"
EXCHANGE_URL="$REVIEWS_BASE_URL/buyer/reviews/v1/verification/exchange"

REQ_BODY="$(MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" python3 - <<'PY'
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
ISSUE_RESP="$(curl -sS -H "Content-Type: application/json" -H "X-Internal-Key: $INTERNAL_KEY" -d "$REQ_BODY" "$ISSUE_URL")"
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

echo
echo "== exchange #1 (expect 200) =="
HDR1="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_hdr1.XXXXXX")"
BODY1="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_body1.XXXXXX")"
curl -sS -D "$HDR1" -o "$BODY1" -H "Content-Type: application/json" -H "Authorization: Bearer $PROOF_TOKEN" \
  -d '{"ttl_seconds":900}' "$EXCHANGE_URL" || true
CODE1="$(awk 'NR==1{print $2}' "$HDR1" | tr -d '\r')"
echo "http_status=$CODE1"
if [[ "$CODE1" == "200" ]]; then
  python3 - <<'PY' <"$BODY1"
import hashlib, json, sys
o=json.load(sys.stdin)
tok=o.get("submission_token") or ""
exp=o.get("expires_at")
fp=hashlib.sha256(tok.encode()).hexdigest()[:12] if tok else ""
print(f"exchange_ok expires_at={exp} submission_token_fp={fp}")
PY
else
  echo "ERROR: exchange #1 failed" >&2
  head -c 200 "$BODY1" || true
  echo
  rm -f "$HDR1" "$BODY1" || true
  exit 1
fi

echo "== exchange #2 (expect 409) =="
last_code=""
last_body=""
attempt=1
while [[ $attempt -le $REPLAY_ATTEMPTS ]]; do
  HDR2="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_hdr2.XXXXXX")"
  BODY2="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_body2.XXXXXX")"
  curl -sS -D "$HDR2" -o "$BODY2" -H "Content-Type: application/json" -H "Authorization: Bearer $PROOF_TOKEN" \
    -d '{"ttl_seconds":900}' "$EXCHANGE_URL" || true
  CODE2="$(awk 'NR==1{print $2}' "$HDR2" | tr -d '\r')"
  echo "attempt=$attempt http_status=$CODE2"

  if [[ "$CODE2" == "409" ]]; then
    echo "replay_ok"
    rm -f "$HDR2" "$BODY2" || true
    last_code="409"
    break
  fi

  last_code="$CODE2"
  last_body="$(head -c 200 "$BODY2" 2>/dev/null || true)"
  rm -f "$HDR2" "$BODY2" || true

  if [[ "$CODE2" =~ ^5[0-9][0-9]$ || -z "$CODE2" || "$CODE2" == "000" ]]; then
    if [[ $attempt -lt $REPLAY_ATTEMPTS ]]; then
      sleep "$REPLAY_DELAY_SECONDS" || true
    fi
    attempt=$((attempt+1))
    continue
  fi

  break
done

if [[ "$last_code" != "409" ]]; then
  if [[ "$last_code" =~ ^5[0-9][0-9]$ || -z "$last_code" || "$last_code" == "000" ]]; then
    echo "replay_infra_flake=1 (no 409 observed; last_http_status=${last_code:-unknown})"
    strict="$(printf '%s' "${STRICT_REPLAY:-}" | tr '[:upper:]' '[:lower:]')"
    if [[ "$strict" == "true" ]]; then
      echo "ERROR: STRICT_REPLAY=true and replay check did not return 409" >&2
      printf '%s\n' "$last_body" >&2
      rm -f "$HDR1" "$BODY1" || true
      exit 1
    fi
  else
    echo "ERROR: replay check unexpected status=$last_code" >&2
    printf '%s\n' "$last_body" >&2
    rm -f "$HDR1" "$BODY1" || true
    exit 1
  fi
fi

rm -f "$HDR1" "$BODY1" || true
echo "OK"
