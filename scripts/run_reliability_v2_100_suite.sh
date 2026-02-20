#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-quick}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  cat <<'EOF'
Usage:
  scripts/run_reliability_v2_100_suite.sh [quick|full] [extra pytest args...]

Modes:
  quick  Reliability-focused regression set under 100% v2 flags
  full   Full pytest under 100% v2 flags

Examples:
  scripts/run_reliability_v2_100_suite.sh
  scripts/run_reliability_v2_100_suite.sh full
  scripts/run_reliability_v2_100_suite.sh quick -x
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONDONTWRITEBYTECODE=1

# 100% enable profile for no-user environments.
# Keep catalog local fallback off for compatibility unless explicitly validating that behavior.
export RELIABILITY_BUDGET_ENABLED=true
export PAYMENT_ROUTING_V2_ENABLED=true
export PAYMENT_ROUTING_V2_MERCHANT_ALLOWLIST=
export CATALOG_RELIABILITY_V2_ENABLED=true
export CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=false
export CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS=1.1

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

case "$MODE" in
  quick)
    echo "== Reliability v2 100% suite (quick) =="
    echo "flags:"
    echo " - RELIABILITY_BUDGET_ENABLED=$RELIABILITY_BUDGET_ENABLED"
    echo " - PAYMENT_ROUTING_V2_ENABLED=$PAYMENT_ROUTING_V2_ENABLED"
    echo " - CATALOG_RELIABILITY_V2_ENABLED=$CATALOG_RELIABILITY_V2_ENABLED"
    echo " - CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=$CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL"
    echo "tests: ${#quick_tests[@]}"
    for t in "${quick_tests[@]}"; do
      echo " - $t"
    done
    python3 -m pytest -q "${quick_tests[@]}" "$@"
    ;;
  full)
    echo "== Reliability v2 100% suite (full) =="
    echo "flags:"
    echo " - RELIABILITY_BUDGET_ENABLED=$RELIABILITY_BUDGET_ENABLED"
    echo " - PAYMENT_ROUTING_V2_ENABLED=$PAYMENT_ROUTING_V2_ENABLED"
    echo " - CATALOG_RELIABILITY_V2_ENABLED=$CATALOG_RELIABILITY_V2_ENABLED"
    echo " - CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=$CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL"
    python3 -m pytest -q "$@"
    ;;
  *)
    echo "Unknown mode: $MODE (expected quick|full)" >&2
    exit 2
    ;;
esac
