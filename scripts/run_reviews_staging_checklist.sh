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
# - RUN_REMOVE_TEST=true|false (default: false)
# - WAIT_FOR_REDEPLOY=true|false (default: false)
#
# Example:
#   REVIEWS_BASE_URL=... PROOF_ISSUER_BASE_URL=... MERCHANT_ID=... PLATFORM_PRODUCT_ID=... ./scripts/run_reviews_staging_checklist.sh

REVIEWS_BASE_URL="${REVIEWS_BASE_URL:-}"
PROOF_ISSUER_BASE_URL="${PROOF_ISSUER_BASE_URL:-}"
MERCHANT_ID="${MERCHANT_ID:-}"
PLATFORM="${PLATFORM:-shopify}"
PLATFORM_PRODUCT_ID="${PLATFORM_PRODUCT_ID:-}"
VARIANT_ID="${VARIANT_ID:-}"
DENY_MERCHANT_ID="${DENY_MERCHANT_ID:-merch_not_allowed}"
METRICS_BEARER_TOKEN="${METRICS_BEARER_TOKEN:-}"
PROOF_ISSUER_INTERNAL_KEY="${PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_PROOF_ISSUER_INTERNAL_KEY:-${REVIEWS_BUYER_PROOF_ISSUER_INTERNAL_KEY:-}}}"
EMPLOYEE_JWT_SECRET_KEY="${EMPLOYEE_JWT_SECRET_KEY:-${JWT_SECRET_KEY:-}}"
RUN_REMOVE_TEST="${RUN_REMOVE_TEST:-false}"
WAIT_FOR_REDEPLOY="${WAIT_FOR_REDEPLOY:-false}"

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

_snapshot_metrics() {
  if [[ -z "${METRICS_BEARER_TOKEN:-}" ]]; then
    echo "metrics_skip=1 (missing METRICS_BEARER_TOKEN)"
    return 0
  fi
  local resp
  resp="$(curl -sS -H "Authorization: Bearer $METRICS_BEARER_TOKEN" "$REVIEWS_BASE_URL/metrics" || true)"
  if [[ -z "$resp" ]]; then
    echo "metrics_skip=1 (empty response)"
    return 0
  fi
  if ! printf '%s' "$resp" | grep -q '^# HELP '; then
    echo "metrics_skip=1 (forbidden or unexpected response)"
    return 0
  fi
  printf '%s' "$resp" | python3 - <<'PY'
import re,sys
text=sys.stdin.read().splitlines()
families=["reviews_buyer_exchange_total","reviews_buyer_create_total","reviews_buyer_media_upload_total"]
out={}
for fam in families:
  total=0.0
  for line in text:
    if not line.startswith(fam+"{") and not line.startswith(fam+" "):
      continue
    m=re.search(r"\s([0-9]+(?:\.[0-9]+)?)$", line)
    if not m:
      continue
    try:
      total += float(m.group(1))
    except Exception:
      pass
  out[fam]=total
print("metrics_ok=1")
for k in families:
  print(f"{k}={out.get(k,0.0)}")
PY
}

echo "== [A2] metrics snapshot (optional) =="
METRICS_BEFORE="$(_snapshot_metrics || true)"
printf '%s\n' "$METRICS_BEFORE"
echo

echo "== [B] entry layer gating + resolve =="
BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/verify_reviews_buyer_canary.sh"
echo

if [[ -z "${PROOF_ISSUER_INTERNAL_KEY:-}" ]]; then
  read -r -s -p "X-Internal-Key (proof issuer): " PROOF_ISSUER_INTERNAL_KEY
  echo
fi

if [[ -z "${EMPLOYEE_JWT_SECRET_KEY:-}" ]]; then
  read -r -s -p "JWT_SECRET_KEY (gcloud secrets versions access latest --secret=env-JWT_SECRET_KEY --project pivota-prod): " EMPLOYEE_JWT_SECRET_KEY
  echo
fi

echo "== [B2] gating deny-case (optional) =="
if [[ "${REQUIRE_DENY_CASE:-}" == "true" ]]; then
  echo "deny_case_enforced=1 (expect write_allowed=false)"
  BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$DENY_MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" EXPECT_WRITE_ALLOWED=false \
    /bin/bash "$(dirname "${BASH_SOURCE[0]}")/verify_reviews_buyer_canary.sh"
else
  echo "deny_case_skip=1 (set REQUIRE_DENY_CASE=true to enforce)"
  # Still run the check for visibility, but do not assert a specific outcome.
  BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$DENY_MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" EXPECT_WRITE_ALLOWED="" \
    /bin/bash "$(dirname "${BASH_SOURCE[0]}")/verify_reviews_buyer_canary.sh" || true
fi
echo

echo "== [C] proof issuer -> exchange replay =="
PROOF_ISSUER_BASE_URL="$PROOF_ISSUER_BASE_URL" PROOF_ISSUER_INTERNAL_KEY="$PROOF_ISSUER_INTERNAL_KEY" REVIEWS_BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/smoke_reviews_proof_issuer_exchange.sh"
echo

echo "== [D] buyer E2E via proof issuer (media + approve + read path) =="
PROOF_ISSUER_BASE_URL="$PROOF_ISSUER_BASE_URL" PROOF_ISSUER_INTERNAL_KEY="$PROOF_ISSUER_INTERNAL_KEY" EMPLOYEE_JWT_SECRET_KEY="$EMPLOYEE_JWT_SECRET_KEY" RUN_REMOVE_TEST="$RUN_REMOVE_TEST" WAIT_FOR_REDEPLOY="$WAIT_FOR_REDEPLOY" REVIEWS_BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
  /bin/bash "$(dirname "${BASH_SOURCE[0]}")/smoke_buyer_review_via_proof_issuer.sh"
echo

if [[ "${RUN_INVITATION_FLOW:-}" == "true" ]]; then
  echo "== [E] buyer E2E via invitation token (optional) =="
  PROOF_ISSUER_BASE_URL="$PROOF_ISSUER_BASE_URL" REVIEWS_BASE_URL="$REVIEWS_BASE_URL" MERCHANT_ID="$MERCHANT_ID" PLATFORM="$PLATFORM" PLATFORM_PRODUCT_ID="$PLATFORM_PRODUCT_ID" VARIANT_ID="$VARIANT_ID" \
    /bin/bash "$(dirname "${BASH_SOURCE[0]}")/smoke_buyer_review_via_invitation.sh"
  echo
fi

echo "== [F] metrics delta (optional) =="
if [[ -n "${METRICS_BEARER_TOKEN:-}" ]]; then
  chmod +x "$(dirname "${BASH_SOURCE[0]}")/check_reviews_buyer_metrics.py" 2>/dev/null || true
  REVIEWS_BASE_URL="$REVIEWS_BASE_URL" METRICS_BEARER_TOKEN="$METRICS_BEARER_TOKEN" \
    python3 "$(dirname "${BASH_SOURCE[0]}")/check_reviews_buyer_metrics.py" || true
else
  echo "metrics_skip=1 (missing METRICS_BEARER_TOKEN)"
fi
echo

echo "ALL OK"
