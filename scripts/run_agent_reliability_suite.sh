#!/usr/bin/env bash
set -euo pipefail

MODE="${1:-quick}"
if [[ $# -gt 0 ]]; then
  shift
fi

if [[ "$MODE" == "-h" || "$MODE" == "--help" ]]; then
  cat <<'EOF'
Usage:
  scripts/run_agent_reliability_suite.sh [quick|full] [extra pytest args...]

Modes:
  quick  Fast regression gate for agent search/auth/shopify hardening (default)
  full   quick + queue/task-manager integration checks

Examples:
  scripts/run_agent_reliability_suite.sh
  scripts/run_agent_reliability_suite.sh full
  scripts/run_agent_reliability_suite.sh quick -x
EOF
  exit 0
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

export PYTHONDONTWRITEBYTECODE=1

quick_tests=(
  "tests/test_external_products.py"
  "tests/test_agent_search_intent.py"
  "tests/test_agent_cart_validate.py"
  "tests/test_agent_product_recommendations.py"
  "tests/test_agent_user_jwt.py"
  "tests/test_shopify_transactions_service.py"
  "tests/test_shopify_order_sync_hardening.py"
  "tests/test_debug_shopify_api_token_parsing.py"
  "tests/test_products_cache_dedupe.py"
)

full_extra_tests=(
  "tests/test_agent_shop_queue_integration.py"
  "tests/test_agent_task_manager.py"
)

selected_tests=()
case "$MODE" in
  quick)
    selected_tests=("${quick_tests[@]}")
    ;;
  full)
    selected_tests=("${quick_tests[@]}" "${full_extra_tests[@]}")
    ;;
  *)
    echo "Unknown mode: $MODE (expected quick|full)" >&2
    exit 2
    ;;
esac

echo "== Agent reliability suite ($MODE) =="
echo "tests: ${#selected_tests[@]}"
for t in "${selected_tests[@]}"; do
  echo " - $t"
done

python3 -m pytest -q "${selected_tests[@]}" "$@"
