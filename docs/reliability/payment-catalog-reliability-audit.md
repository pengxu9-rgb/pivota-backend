# Payment + Product Catalog Reliability Audit (Plan v2)

## Scope
- Services:
  - Payment routing and PSP failover (`services/payment_routing_service.py`)
  - Product catalog search and shopping fallback (`routes/agent_api.py`, `routes/agent_shop_gateway.py`, `services/product_query_service.py`)
- Guardrail:
  - No breaking change to existing external API contracts.
  - All new reliability strategies default to `off` except explicit bugfixes.

## Findings and Remediation

### P0: Unbounded/ambiguous failover behavior under errors
- Symptoms:
  - Retry/fallback behavior could become hard to reason about under repeated PSP failure.
  - Missing explicit attempt cap in enhanced path and incomplete no-backjump protections.
- Remediation:
  - Added v2 payment state-machine controls (default off): total attempt cap, visited PSP set, no backjump, retry/fallback error classification, cooldown pacing, in-memory circuit behavior.
  - Fixed edge cases for empty route priority, final `last_psp` reporting, and fallback metric/log emission.
- Files:
  - `services/payment_routing_service.py`
  - `tests/test_payment_routing_reliability.py`

### P0: Timeout budget not standardized end-to-end
- Symptoms:
  - Realtime catalog calls had fixed timeout but no shared budget primitive for chain-level control.
- Remediation:
  - Added `RequestBudget` utility and budget-aware timeout derivation.
  - Added budget-gated behavior in product query path; default remains off (`RELIABILITY_BUDGET_ENABLED=false`).
  - Added request-id propagation hook into merchant adapter headers.
- Files:
  - `core/reliability/budget.py`
  - `services/product_query_service.py`
  - `adapters/merchant_api_adapter.py`
  - `tests/test_product_query_service.py`

### P0: Catalog shopping fallback could thrash under upstream instability
- Symptoms:
  - Delegate-to-upstream fallback path risked repeated failures with weak state control.
- Remediation:
  - Added v2 catalog reliability controls (default off): stricter threshold/circuit settings, timeout cap selection, optional local fallback only when explicitly enabled and budget allows.
  - Added helper gate to keep existing behavior when flags are off.
- Files:
  - `routes/agent_shop_gateway.py`
  - `tests/test_catalog_reliability_v2_flags.py`

### P1: Observability gaps across retry/fallback paths
- Symptoms:
  - Hard to diagnose by PSP/path which failure type dominated.
- Remediation:
  - Added reliability metrics module (counter/histogram/gauge with no-op fallback).
  - Added key path emissions in payment and catalog routes.
  - Added structured payment routing logs with attempt/failure/success/exhausted events.
- Files:
  - `observability/reliability_metrics.py`
  - `services/payment_routing_service.py`
  - `routes/agent_api.py`
  - `routes/agent_shop_gateway.py`

### P0 bugfix: SDK delegation semantic drift
- Symptoms:
  - `allow_external_seed=false` was not fully honored in one merge path.
  - `fast_mode` delegation could be inconsistent for SDK handler.
- Remediation:
  - Respect `allow_external_seed` in SDK external seed merge condition.
  - Normalize `fast_mode` in `agent_api` direct invocation path.
  - Pass through SDK `fast_mode` query param to delegated `agent_api` search handler.
- Files:
  - `routes/agent_sdk_fixed.py`
  - `routes/agent_api.py`
  - `tests/test_external_products.py`
  - `tests/test_agent_search_fast_mode.py`

## Compatibility and Stability Guardrails
- No route or method changes.
- No required new client parameters.
- Existing response shapes preserved for:
  - `/agent/v1/products/search`
  - `/agent/shop/v1/invoke`
  - `/agent/v1/payments`
- New strategy switches are default off:
  - `RELIABILITY_BUDGET_ENABLED=false`
  - `PAYMENT_ROUTING_V2_ENABLED=false`
  - `CATALOG_RELIABILITY_V2_ENABLED=false`

## Validation

### Contract tests
- Added:
  - `tests/contracts/test_agent_contracts.py`

### Reliability/state-machine tests
- Added:
  - `tests/test_payment_routing_reliability.py`
  - `tests/test_catalog_reliability_v2_flags.py`
  - budget on/off coverage in `tests/test_product_query_service.py`

### Execution result
- Full suite:
  - `python3 -m pytest -q`
  - Result: `296 passed`
