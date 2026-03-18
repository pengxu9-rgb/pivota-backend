# Status

As of March 18, 2026, the one-merchant readiness alpha is implemented for `merch_efbc46b4619cfbdf` behind feature flags and validated in production on Railway `Pivota Infra / production / web`.

Implemented:

- real Shopify-backed readiness source for one merchant
- explicit source-of-truth contract for seven field families
- additive Reviews Center projection into readiness report/export/scoring
- canonical readiness-owned checkout/order-sync path
- machine-readable report/export/checkout/order-sync responses with blockers, warnings, provenance, and freshness
- lightweight `summary_only=true` report/export modes for internal ops and production smoke
- explicit readiness error contract preserved through the global error middleware
- read-only order-sync audit that aggregates readiness journal, `orders`, Shopify webhook ingest, `refund_records`, and `return_records`
- replay-based convergence from downstream merchant cancellation/refund state back into readiness checkout session state
- synthetic regression path preserved
- captured real-merchant fixtures and regression tests

Validated:

- `python3 -m py_compile` on the readiness modules and router
- `python3 -m pytest readiness/tests -q`
- result after current error-contract + summary-only work: `24 passed`
- targeted follow-up validation for sync-audit work:
  - `python3 -m pytest readiness/tests/test_sync_audit.py readiness/tests/test_routes.py tests/test_error_handler.py -q`
  - `bash -n scripts/smoke_readiness_alpha.sh`
- targeted follow-up validation for replay convergence:
  - `python3 -m pytest readiness/tests/test_sync_audit.py readiness/tests/test_routes.py -q`
- production deploy progression:
  - `ad49b8c`: hardened review fallback logic
  - `e20086b`: switched readiness review aggregates to raw SQL
  - `078b46c`: added summary-only report/export views
  - `6fe88e1`: preserved readiness top-level error codes
  - `f29000c`: expanded readiness error-contract coverage and smoke assertions
- production summary-only smoke against `https://web-production-fedb.up.railway.app`
  - readiness report summary: `200`
  - UCP export summary: `200`
  - blocked checkout probe: `409`
  - top-level blocked checkout error code: `VARIANT_NOT_READY_FOR_CHECKOUT`
  - live source-of-truth outcome:
    - `catalog=shopify_cache.standard_product.v1`
    - `price=shopify_admin.products.v2024-07`
    - `inventory=shopify_admin.inventory.v2024-07`
    - `reviews_confidence=reviews_center.review_group.v1`
  - observed production summary report:
    - `readiness_score=77`
    - `product_count=740`
    - `variant_count=2363`
    - `ready_variant_count=2098`
    - `blocked_variant_count=265`
    - `products_with_reviews=737`
  - observed production summary export:
    - `readiness_score=88`
    - `offer_count=2098`
    - `review_backed_offer_count=2096`
  - dominant remaining blockers: `out_of_stock`, `missing_price`
- supervised production canary write on March 17, 2026
  - checkout create: `200`
  - checkout id: `rdchk_4b7c7a42214f4bf0`
  - order sync advance: `200`
  - local order id: `ORD_568F2F4E7FC37F33`
  - merchant write-back event: `order_forwarded_to_merchant`
  - Shopify order id: `7472359801160`
  - Shopify order name: `#1041`
  - final readiness state: `state_synced`
  - replay result: `200`, `replayed=true`, no duplicate event types observed

Not yet executed live:

- no direct PSP authorize/capture executed by the readiness router itself
- no live webhook/reconciliation validation for readiness order state
- no live production re-smoke yet for the new Reviews Center projection

Major remaining risks:

- readiness now projects product-level review summaries from Reviews Center, but broader review freshness/ranking/coverage still needs convergence
- merchant-native payment execution exists in the platform, but the readiness router still uses capability check + merchant order write-back instead of a fully unified PSP execution step
- merchant fulfillment/returns policy is still manual config, not live-ingested
- full report/export payloads remain large when `summary_only` is not used
- refund and return sync still need a live exercise; cancellation sync is now live-validated but refund remains blocked for unpaid readiness-alpha canary orders
