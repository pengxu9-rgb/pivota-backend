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

PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"

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

INTERNAL_KEY="${PROOF_ISSUER_INTERNAL_KEY:-}"
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
  head -c 200 "$BODY1" || true
  echo
fi

echo "== exchange #2 (expect 409) =="
HDR2="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_hdr2.XXXXXX")"
BODY2="$(mktemp "${TMPDIR:-/tmp}/proof_exchange_body2.XXXXXX")"
curl -sS -D "$HDR2" -o "$BODY2" -H "Content-Type: application/json" -H "Authorization: Bearer $PROOF_TOKEN" \
  -d '{"ttl_seconds":900}' "$EXCHANGE_URL" || true
CODE2="$(awk 'NR==1{print $2}' "$HDR2" | tr -d '\r')"
echo "http_status=$CODE2"
if [[ "$CODE2" == "409" ]]; then
  echo "replay_ok"
else
  head -c 200 "$BODY2" || true
  echo
fi

rm -f "$HDR1" "$BODY1" "$HDR2" "$BODY2" || true
echo "OK"
