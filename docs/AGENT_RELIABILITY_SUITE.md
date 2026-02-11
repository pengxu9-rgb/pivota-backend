# Agent Reliability Suite

## Goal

Provide a stable regression gate for the most failure-prone agent paths:

- Product search relevance and merchant scoping
- External seed injection boundaries
- Agent auth/JWT behavior
- Shopify token parsing and order/sync hardening
- Cache dedupe correctness

## Commands

From repo root:

```bash
scripts/run_agent_reliability_suite.sh
```

Equivalent to `quick` mode.

```bash
scripts/run_agent_reliability_suite.sh full
```

Runs quick suite plus queue/task-manager integration checks.

## Suite Composition

### Quick

- `tests/test_external_products.py`
- `tests/test_agent_search_intent.py`
- `tests/test_agent_cart_validate.py`
- `tests/test_agent_product_recommendations.py`
- `tests/test_agent_user_jwt.py`
- `tests/test_shopify_transactions_service.py`
- `tests/test_shopify_order_sync_hardening.py`
- `tests/test_debug_shopify_api_token_parsing.py`
- `tests/test_products_cache_dedupe.py`

### Full (quick +)

- `tests/test_agent_shop_queue_integration.py`
- `tests/test_agent_task_manager.py`

## CI Gate Recommendation

Use `quick` as required PR gate.
Run `full` on nightly or pre-release branches.

## Notes

- The suite is intentionally deterministic and avoids external network dependency.
- If adding new agent search/routing logic, include a test in this suite before merge.
