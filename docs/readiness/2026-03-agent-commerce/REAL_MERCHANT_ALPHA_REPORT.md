# Real Merchant Alpha Report

## Merchant

- merchant id: `merch_efbc46b4619cfbdf`
- platform: Shopify
- alpha mode: `real_merchant_alpha`

## Current Alpha Readiness

Live production observation on March 17, 2026:

- readiness score: `70` on the report path
- ready variants: `2098`
- blocked variants: `265`
- checkout capability: `ready`
- order-sync capability: `ready`
- export offer count: `2098`
- live source-of-truth:
  - `catalog=shopify_cache.standard_product.v1`
  - `price=shopify_admin.products.v2024-07`
  - `inventory=shopify_admin.inventory.v2024-07`

Supervised production canary write:

- checkout id: `rdchk_4b7c7a42214f4bf0`
- local order id: `ORD_568F2F4E7FC37F33`
- merchant order id: `7472359801160`
- merchant order name: `#1041`
- final order-sync state: `state_synced`
- replay behavior: `replayed=true` with no duplicate event types

Captured fixture expectation kept for regression:

- readiness score: `76`
- ready variants: `431000000001`, `431000000002`, `431000000003`
- blocked variants: `431000000004`
- checkout capability: `ready`
- order-sync capability: `ready`
- reviews/confidence capability: `ready`

## Primary Blockers Still Visible

- readiness now projects product-level Reviews Center summaries, but live production has not yet been re-smoked after this projection change
- merchant-native payment execution exists in the platform, but the readiness router still does not own direct PSP authorize/capture
- live blocked variants are now dominated by `out_of_stock` and `missing_price`, not stale inventory snapshots

## Evidence

- machine-readable summary fixture:
  - `readiness/fixtures/golden_real_merchant_readiness_report_ucp.json`
- machine-readable export summary:
  - `readiness/fixtures/golden_real_merchant_ucp_export.json`
- representative blocked checkout:
  - `readiness/fixtures/golden_real_merchant_blocked_checkout.json`
- representative successful order-sync:
  - `readiness/fixtures/golden_real_merchant_order_sync.json`
- live production artifacts from supervised validation:
  - `/tmp/pivota-readiness-direct-canary-20260317/checkout.json`
  - `/tmp/pivota-readiness-direct-canary-20260317/order_sync.json`
  - `/tmp/pivota-readiness-direct-canary-20260317/order_sync_replay.json`

## Local Validation Caveat

This workspace did not have a local live `DATABASE_URL` on March 17, 2026. Live validation therefore ran against the deployed Railway production service instead of a local DB-backed process.
