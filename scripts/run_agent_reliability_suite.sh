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
  "tests/test_public_api_base_url.py"
  "tests/test_legacy_backend_url_guard.py"
  # Rig/demo exclusion policy. THESE THREE FILES were in no CI job.
  #
  # Be precise about the scope of that claim, because the imprecise version
  # sends the next reader hunting in the wrong place: `tests/services/` as a
  # DIRECTORY is not invisible — .github/workflows/v3-audit-gate.yml names 20
  # files in it explicitly. What is true is narrower and worse: every gate in
  # this repo selects tests by a FIXED, hand-maintained list (this array,
  # Checkout Payment Safety's own list, v3-audit-gate's list) except
  # postgres-dialect-gate, which globs only `tests/test_*_postgres.py`. So a new
  # test file is gated only if somebody remembers to name it somewhere, and
  # these three were named nowhere.
  #
  # That matters more here than for an average test: their whole job is to be
  # the tripwire against a rig reappearing, and the last rig reappeared exactly
  # because the policy moved in some files and not others. A tripwire wired to
  # nothing cannot fail — the failure mode this repo keeps rediscovering.
  #
  # All three are the same lockstep policy named in services/test_merchant_policy.py:
  #   test_test_merchant_policy.py    — the serving predicate itself
  #   test_step5_working_set.py       — the analytics twin (DEMO_MERCHANT_IDS)
  #   test_identity_reconcile_sweep.py — DEMO_EXCLUSION_SQL reaching the sweep gauges
  #
  # ~1s for the three. No DB. (Not "pure Python" — test_step5_working_set
  # transitively imports httpx and pydantic_settings through
  # services.pdp_matcher. Fine in CI, but it is an import chain, not a leaf.)
  # This workflow already triggers on services/** and scripts/**.
  "tests/services/test_test_merchant_policy.py"
  "tests/services/test_step5_working_set.py"
  "tests/services/test_identity_reconcile_sweep.py"
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
