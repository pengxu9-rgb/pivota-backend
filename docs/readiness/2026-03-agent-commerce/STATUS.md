# Status

As of March 17, 2026, the one-merchant readiness alpha is implemented for `merch_efbc46b4619cfbdf` behind feature flags.

Implemented:

- real Shopify-backed readiness source for one merchant
- explicit source-of-truth contract for seven field families
- additive Reviews Center projection into readiness report/export/scoring
- canonical readiness-owned checkout/order-sync path
- machine-readable report/export/checkout/order-sync responses with blockers, warnings, provenance, and freshness
- synthetic regression path preserved
- captured real-merchant fixtures and regression tests

Validated:

- `python3 -m py_compile` on the readiness modules and router
- `python3 -m pytest readiness/tests -q`
- result: `12 passed`
- production deploy on Railway `Pivota Infra / production / web` at commit `20a40a74f2d1531403e898dde3c899ce50ef8ac0`
- production read-only smoke against `https://web-production-fedb.up.railway.app`
  - readiness report: `200`
  - UCP export: `200`
  - blocked checkout probe: `409 VARIANT_NOT_READY_FOR_CHECKOUT`
  - live source-of-truth outcome:
    - `catalog=shopify_cache.standard_product.v1`
    - `price=shopify_admin.products.v2024-07`
    - `inventory=shopify_admin.inventory.v2024-07`
  - observed production alpha summary:
    - `product_count=740`
    - `ready_variant_count=2098`
    - `blocked_variant_count=265`
    - `offer_count=2098`
    - dominant remaining blockers: `out_of_stock`, `missing_price`
- production hotfix deploy on Railway `Pivota Infra / production / web` at commit `f0ba0419749306e772e5744f30983e3bee4283b2`
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
- the smoke script summary is too verbose for large merchants unless artifacts are inspected directly
- webhook-driven reconciliation and refund/cancel sync remain unvalidated on the live alpha path
