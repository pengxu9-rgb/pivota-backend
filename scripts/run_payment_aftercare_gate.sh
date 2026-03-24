#!/usr/bin/env bash

set -euo pipefail

BACKEND_REPO="${PIVOTA_BACKEND_REPO:-/Users/pengchydan/dev/Pivota-cursor-create-project-directory-structure-8344/pivota-backend}"

if [[ ! -d "$BACKEND_REPO" ]]; then
  echo "missing backend repo directory: $BACKEND_REPO" >&2
  exit 1
fi

cd "$BACKEND_REPO"

python3 -m pytest \
  tests/test_checkout_webhook_contract.py \
  tests/test_stripe_webhook_contract.py \
  tests/test_adyen_webhook_contract.py \
  tests/test_payment_aftercare_ordering.py \
  tests/test_webhook_service_resilience.py \
  -q

echo
echo "payment aftercare gate passed"
