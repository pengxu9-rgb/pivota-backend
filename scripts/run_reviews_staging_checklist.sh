#!/bin/bash
set -euo pipefail

# One-shot staging checklist runner for Reviews buyer submission + proof issuer.
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
# - PROOF_ISSUER_INTERNAL_KEY (otherwise prompted by child script)
# - EMPLOYEE_JWT_SECRET_KEY or JWT_SECRET_KEY (otherwise prompted by child script)
# - MEDIA_FILE_PATH or MOCK_MEDIA_DIR
#
# Example:
#   REVIEWS_BASE_URL=... PROOF_ISSUER_BASE_URL=... MERCHANT_ID=... PLATFORM_PRODUCT_ID=... ./scripts/run_reviews_staging_checklist.sh

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"

command -v curl >/dev/null 2>&1 || { echo "ERROR: curl not found" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found" >&2; exit 1; }

if [[ -z "$REVIEWS_BASE_URL" ]]; then
  echo "ERROR: missing REVIEWS_BASE_URL" >&2
  exit 1
fi
if [[ -z "$PROOF_ISSUER_BASE_URL" ]]; then
  echo "ERROR: missing PROOF_ISSUER_BASE_URL" >&2
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

echo "== [A] service health/build =="
curl -sS "$PROOF_ISSUER_BASE_URL/health"
echo
curl -sS "$PROOF_ISSUER_BASE_URL/__build" | head -c 240 || true
echo
curl -sS "$REVIEWS_BASE_URL/__build" | head -c 240 || true
echo

echo "== [B] entry layer gating + resolve =="
BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/verify_reviews_buyer_canary.sh"
echo

echo "== [C] proof issuer -> exchange replay =="
PROOF_ISSUER_BASE_URL="$PROOF_ISSUER_BASE_URL" REVIEWS_BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/smoke_reviews_proof_issuer_exchange.sh"
echo

echo "== [D] buyer E2E via proof issuer (media + approve + read path) =="
PROOF_ISSUER_BASE_URL="$PROOF_ISSUER_BASE_URL" REVIEWS_BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/smoke_buyer_review_via_proof_issuer.sh"
echo

echo "ALL OK"
