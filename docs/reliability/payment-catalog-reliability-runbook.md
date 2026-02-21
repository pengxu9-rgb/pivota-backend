# Payment + Catalog Reliability Runbook

## 1) Feature Flags (default-safe)

### Budget / timeout propagation
- `RELIABILITY_BUDGET_ENABLED=false`
- `AGENT_PRODUCTS_TOTAL_BUDGET_MS=2500`
- `PAYMENT_TOTAL_BUDGET_MS=3500`

### Payment routing v2
- `PAYMENT_ROUTING_V2_ENABLED=false`
- `PAYMENT_ROUTING_V2_MERCHANT_ALLOWLIST=""`
- `PAYMENT_ROUTING_MAX_ATTEMPTS_TOTAL=3`
- `PAYMENT_ROUTING_COOLDOWN_SECONDS=60`
- `PAYMENT_ROUTING_CIRCUIT_FAILURE_THRESHOLD=3`
- `PAYMENT_ROUTING_CIRCUIT_WINDOW_SECONDS=45`
- `PAYMENT_ROUTING_CIRCUIT_OPEN_SECONDS=60`
- `PAYMENT_ROUTING_HALF_OPEN_PROBES=1`

### Catalog reliability v2
- `CATALOG_RELIABILITY_V2_ENABLED=false`
- `CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=false`
- `CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS=1.2`
- `CATALOG_UPSTREAM_V2_CIRCUIT_FAILURE_THRESHOLD=3`
- `CATALOG_UPSTREAM_V2_CIRCUIT_WINDOW_SECONDS=45`
- `CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_SECONDS=60`
- `CATALOG_UPSTREAM_V2_CIRCUIT_OPEN_ON_TIMEOUT=true`
- `CATALOG_UPSTREAM_V2_LOCAL_FALLBACK_MIN_BUDGET_SECONDS=0.4`

## 2) Rollout
- Recommended sequence:
  1. Keep all new strategy flags `off`; deploy observability first.
  2. Enable per allowlist merchant/surface.
  3. Ramp `1% -> 10% -> 25% -> 50% -> 100%`.
  4. Hold each stage at least 30 minutes.
- Abort criteria:
  - Error rate > baseline + `0.2pp`
  - p95 latency > baseline + `10%`
  - timeout/fallback spikes vs baseline

### 2.1 No-user environment: direct 100% validation
- Use this only in environments with no real user traffic.
- Compatibility-safe 100% profile:
  - `RELIABILITY_BUDGET_ENABLED=true`
  - `PAYMENT_ROUTING_V2_ENABLED=true`
  - `PAYMENT_ROUTING_V2_MERCHANT_ALLOWLIST=""`
  - `CATALOG_RELIABILITY_V2_ENABLED=true`
  - `CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=false`
  - `CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS=1.1`
- One-command test run:
  - `scripts/run_reliability_v2_100_suite.sh quick`
  - `scripts/run_reliability_v2_100_suite.sh full`
- Endpoint smoke (non-pytest):
  - `scripts/smoke_reliability_v2_100_endpoints.sh`
- Config template:
  - `docs/reliability/reliability-v2-100.env.example`

## 3) Instant Rollback (config-only)

Set all v2 strategy flags off:

```bash
RELIABILITY_BUDGET_ENABLED=false
PAYMENT_ROUTING_V2_ENABLED=false
CATALOG_RELIABILITY_V2_ENABLED=false
CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=false
CATALOG_UPSTREAM_V2_SHOPPING_TIMEOUT_CAP_SECONDS=1.2
```

If using allowlist rollout, clear allowlists and redeploy config only.

Config template:
- `docs/reliability/reliability-v2-rollback.env.example`

Rollback drill command:
- `scripts/drill_reliability_v2_rollback.sh`

## 4) Metrics to Watch

### Payment
- `payment_attempt_total{psp,result,error_category}`
- `payment_fallback_total{from_psp,to_psp,reason}`
- `payment_timeout_total{psp,stage}`
- `payment_circuit_state{psp,state}`
- `payment_latency_seconds{psp,result}`
- `retry_attempts_total{domain="payment",category}`

### Catalog
- `catalog_search_requests_total{mode,path,result}`
- `catalog_search_latency_seconds{path,result}`
- `catalog_upstream_fallback_total{reason}`
- `catalog_upstream_timeout_total{surface}`
- `catalog_upstream_circuit_state{surface,state}`

## 5) Log Events / Key Fields

### Payment routing logs
- `payment.routing.attempt`
  - `mode`, `order_id`, `route_id`, `attempt_number`, `attempt_limit`, `psp`, `visited_psps`
- `payment.routing.failure`
  - `error_category`, `retryable`, `fallbackable`, `reason`, `error_message`
- `payment.routing.success`
  - `response_time_ms`, `attempt_number`, `psp`
- `payment.routing.exhausted`
  - `attempts`, `attempt_limit`, `visited_psps`, `last_psp`, `error`

### Catalog upstream logs
- `multi.upstream_fallback.http_error`
  - `status_code`, `url`
- `multi.upstream_fallback.failed`
  - `error`, `url`
- `multi.upstream_fallback.local_fallback_enabled`
  - `reason`, `source`

## 6) Triage Playbooks

### A. Payment failures spike for a PSP
1. Check `payment_timeout_total` and `payment_attempt_total` by PSP.
2. Check `payment_circuit_state` for frequent `open`.
3. Verify `payment.routing.failure` categories; if mostly business declines, do not increase retries.
4. If needed, disable v2 (`PAYMENT_ROUTING_V2_ENABLED=false`) for immediate stabilization.

### B. Catalog shopping timeout/fallback spike
1. Check `catalog_upstream_timeout_total{surface="shopping"}` and `catalog_upstream_fallback_total`.
2. Inspect `multi.upstream_fallback.failed` logs for timeout vs payload failures.
3. If local fallback should be preserved, enable:
   - `CATALOG_RELIABILITY_V2_ENABLED=true`
   - `CATALOG_RELIABILITY_V2_LOCAL_FALLBACK_ON_DELEGATE_FAIL=true`
4. If instability persists, disable v2 catalog flags and keep delegate mode conservative.

## 7) Contract Regression Checklist
- `/agent/v1/products/search`:
  - status code, required fields, field types unchanged.
- `/agent/shop/v1/invoke`:
  - `find_products_multi` response shape unchanged.
- `/agent/v1/payments`:
  - status + payment identifiers + amount/currency/psp fields unchanged.
