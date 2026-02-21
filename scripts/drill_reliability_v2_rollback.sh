#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONDONTWRITEBYTECODE=1

quick_tests=(
  "tests/test_payment_routing_reliability.py"
  "tests/test_catalog_reliability_v2_flags.py"
  "tests/contracts/test_agent_contracts.py"
  "tests/test_product_query_service.py"
  "tests/test_external_products.py"
  "tests/test_agent_shop_queue_integration.py"
  "tests/test_quote_first_replay_idempotency.py"
  "tests/test_agent_search_fast_mode.py"
  "tests/test_cors.py"
)

echo "== Rollback drill: v2 ON profile =="
scripts/run_reliability_v2_100_suite.sh quick

echo
echo "== Rollback drill: v2 OFF profile (config rollback simulation) =="
RELIABILITY_BUDGET_ENABLED=false \
PAYMENT_ROUTING_V2_ENABLED=false \
CATALOG_RELIABILITY_V2_ENABLED=false \
CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=false \
python3 -m pytest -q "${quick_tests[@]}"

echo
echo "ROLLBACK_DRILL_OK"
