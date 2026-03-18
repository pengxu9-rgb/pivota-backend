# Real Merchant Alpha Report

## Merchant

- merchant id: `merch_efbc46b4619cfbdf`
- platform: Shopify
- alpha mode: `real_merchant_alpha`

## Current Alpha Readiness

Live production observation on March 18, 2026:

- report summary readiness score: `77`
- export summary readiness score: `88`
- ready variants: `2098`
- blocked variants: `265`
- checkout capability: `ready`
- order-sync capability: `ready`
- reviews/confidence capability: `ready`
- export offer count: `2098`
- review-backed exported offers: `2096`
- live source-of-truth:
  - `catalog=shopify_cache.standard_product.v1`
  - `price=shopify_admin.products.v2024-07`
  - `inventory=shopify_admin.inventory.v2024-07`
  - `reviews_confidence=reviews_center.review_group.v1`
- live summary blocker mix:
  - checkout blockers: `out_of_stock=217`, `missing_price=37`
  - discovery blockers: `missing_price=37`, `missing_primary_image=12`

Supervised production canary write:

- checkout id: `rdchk_4b7c7a42214f4bf0`
- local order id: `ORD_568F2F4E7FC37F33`
- merchant order id: `7472359801160`
- merchant order name: `#1041`
- final order-sync state: `state_synced`
- replay behavior: `replayed=true` with no duplicate event types

Supervised production payment-intent canary on March 18, 2026:

- checkout id: `rdchk_cce46acd5fc340c1`
- local order id: `ORD_55131C19D6DE97BB`
- merchant order id: `7473638277448`
- PSP used: `stripe`
- payment-intent creation result: `awaiting_payment`
- payment-intent replay behavior: `replayed=true` with the same `payment_intent_id`
- post-intent audit:
  - `merchant_writeback=ready`
  - `webhook_ingest=ready`
  - `refund_sync=not_eligible`
  - refund ineligibility reason: `order_not_paid`
- the next readiness-owned bridge point is `payment-status-sync`, which can poll the PSP for that `payment_intent_id` and only auto-mark paid if the PSP reports a real successful terminal state
- production spot-check on that route succeeded on March 18, 2026:
  - missing checkout returned `CHECKOUT_NOT_FOUND`
  - existing Stripe-backed checkout `rdchk_cce46acd5fc340c1` returned `payment_intent_status=requires_payment_method`
  - readiness normalized that to `awaiting_payment`
  - no false paid bridge occurred

Captured fixture expectation kept for regression:

- readiness score: `76`
- ready variants: `431000000001`, `431000000002`, `431000000003`
- blocked variants: `431000000004`
- checkout capability: `ready`
- order-sync capability: `ready`
- reviews/confidence capability: `ready`

## Primary Blockers Still Visible

- readiness now projects product-level Reviews Center summaries successfully, but grouped review coverage is still `0` in the current merchant observation
- merchant-native payment execution exists in the platform, but the readiness router still does not own direct PSP authorize/capture
- live blocked variants are now dominated by `out_of_stock` and `missing_price`, not stale inventory snapshots
- full report/export payloads are still expensive unless internal consumers use `summary_only=true`
- refund / cancel / return sync still require a controlled live exercise; the new order-sync audit only surfaces their evidence paths and current observation state
- readiness now has an internal `payment-bridge` surface that can attach an externally successful PSP reference to the readiness-owned order and make refund validation meaningful without refactoring the platform payment stack first
- readiness now also has an internal `payment-intent` surface that can mint a PSP payment intent directly for the readiness-owned local order
- that `payment-intent` surface is now live-validated for intent creation and idempotent replay, but not yet for confirmed payment -> paid bridge -> refund

Follow-up live validation on March 18, 2026:

- merchant-side cancellation was successfully exercised on canary Shopify order `7473593680200`
- `order-sync-audit` confirmed:
  - `webhook_ingest=ready`
  - `cancellation_sync=ready`
  - `orders.status=cancelled`
- this exposed one additional gap that is now closed in code: readiness replay must absorb downstream merchant-side cancellation/refund state back into the readiness checkout session

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
- live production summary smoke:
  - `/tmp/pivota-readiness-smoke-20260318T000801Z/report.json`
  - `/tmp/pivota-readiness-smoke-20260318T000801Z/export_ucp.json`

## Error Contract

Current production readiness error surface now preserves explicit top-level codes through the global error middleware:

- blocked checkout:
  - HTTP `409`
  - `error.code=VARIANT_NOT_READY_FOR_CHECKOUT`
- unsupported merchant:
  - HTTP `404`
  - `error.code=READINESS_MERCHANT_UNSUPPORTED`
- missing checkout session:
  - HTTP `404`
  - `error.code=CHECKOUT_NOT_FOUND`

## Post-Order Audit

Readiness now exposes a read-only post-order audit:

- `GET /internal/readiness/merchants/{merchant_id}/order-sync-audit/{checkout_id}`

The audit is intended to validate the convergence of:

- readiness journal state
- local `orders` row
- `pcs_shopify_webhook_events`
- `refund_records`
- `return_records`

For the current alpha merchant, this makes cancellation/refund/return validation operationally tractable without changing the canonical checkout path.

## Payment Bridge

New internal surface:

- `POST /internal/readiness/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-bridge`

Current intent:

- bridge an already-successful PSP payment reference into the readiness alpha order
- mark the local `orders` row `paid`
- best-effort sync the external payment reference into the linked Shopify order transaction list
- make `refund_sync.refund_eligible=true` in the post-order audit before a controlled refund test

New internal surface:

- `POST /internal/readiness/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-intent`

Current intent:

- let readiness own PSP payment-intent creation for the alpha order path
- reuse the existing multi-PSP orchestration layer without rerouting public payment APIs
- remove the operator dependency on separately sourcing a payment intent before later refund validation

## Local Validation Caveat

This workspace did not have a local live `DATABASE_URL` on March 17, 2026. Live validation therefore ran against the deployed Railway production service instead of a local DB-backed process.
