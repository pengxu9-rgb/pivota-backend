# Shopify Discount Audit

## Update: 2026-04-21 current merchant rerun

For the current merchant `merch_efbc46b4619cfbdf`, three conclusions in the original audit have changed materially:

1. Shopify Admin GraphQL discount-node sync is no longer blocked. Internal sync updated `6` live discount nodes successfully.
2. Automatic discount execution is no longer fixture-blocked. `Pivota Auto Test` is now live-proven on an in-scope product with no code.
3. Positive combinability is no longer pending. `PIVOTA_TEST_BXGY + PIVOTA_TEST_COMBO_B` is live-proven as a successful product-plus-order combination for the current merchant.

Additional repair work since the original audit:

- Storefront cart-level order-code allocations are now normalized as `discount_class=order` instead of being misclassified as `product`.
- Quote-level `store_discount_evidence.offers` is deduped across multi-item carts.
- Product-detail `store_discount_evidence.offers` and `decisions` are deduped across variants, which removes repeated savings rows on PDP payloads.

Current remaining gaps for this merchant:

- No Shopify-native discount execution gap remains open in the audited matrix. Remaining rollout limits are operational: merchant/PSP-specific `fail_closed` canaries, non-Stripe PSP adapter completeness, and ongoing order-create/refund monitoring.

## Executive conclusion

The Shopify discount integration is no longer just cosmetic. The quote-time path now has live evidence for Shopify-native code acceptance/rejection, product discount allocation parsing, paid shipping, free-shipping netting, BXGY quantity gating, and basic conflict handling. The current production build reads Shopify Storefront `discountCodes` and `discountAllocations`, carries normalized discount evidence into quote snapshots, suppresses overlapping Pivota manual promotions, and now prevents a rejected Shopify code from being silently replaced by a local manual promo. Evidence is in `services/shopify_storefront_pricing_service.py:355-623`, `services/quote_service.py:437-449`, `services/quote_service.py:641-658`, and the live artifacts under `artifacts/shopify-discount-validation/`.

The paid path is now live-proven for this explicitly approved Stripe + Shopify test merchant across three refunded discounted orders and one additional usage-limit redemption order: free shipping (`ORD_508D4460ACA8DE11`), amount-off (`ORD_E2CC099ACF7A88A7`), BXGY (`ORD_F56E0A1E5DC79E82`), and usage-limit redemption (`ORD_B27BD95A214B40D4`). The refund cleanup exposed and fixed three real defects: agent refund idempotency, Stripe Checkout Session refund resolution, and Shopify refund-webhook double counting.

This is no longer blocked on Shopify discount-node access for the current merchant. The merchant token now has `read_discounts`, `write_discounts`, and `read_customers`; internal fixture creation was used to build live Shopify-native fixed-amount, segment, new-customer, usage-limit, and active-window test discounts, and sync/writeback remained healthy. The last usage-limit gap is now closed as well: order `ORD_B27BD95A214B40D4` redeemed `PIVOTA_AUDIT_20260421A_LIMIT1`, and the post-redemption quote rerun returned `applicable=false`, `discount_total=0`, and `customer_eligibility.shopify_order_count=1` (`/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/usage-limit-redemption/03-post-redemption-quote.response.json`).

One important truth point: the fixture named `PIVOTA_TEST_AMOUNT10` is not evidence of fixed-amount discount support. Live behavior on a `29.00` item produced a `2.90` discount, so the validated fixture is a 10% product discount, not a fixed `10` amount discount (`artifacts/shopify-discount-validation/live-us-shipping-final-retest-20260415T051842Z/summary.json:175-214`).

## Current Shopify discount integration map

### 1. Where discount definitions originate

- Shopify-native merchant discounts are read into Pivota promotions through GraphQL `discountNodes`, now the default sync path when `SHOPIFY_DISCOUNT_GRAPHQL_SYNC=1`:
  - `services/shopify_promotions_sync.py:339-358`
  - `services/shopify_promotions_sync.py:384-470`
  - `services/shopify_promotions_sync.py:621-700`
- Synced metadata is stored as `promotions.config` with source `shopify_discount_node`, method, type, combinesWith, context, customerGets/customerBuys, minimumRequirement, usageLimit, appliesOncePerCustomer, and codes:
  - `services/shopify_promotions_sync.py:339-358`
- Open-ended Shopify discounts are preserved by making `promotions.end_at` nullable:
  - `db/migrations/062_shopify_discount_open_ended_promotions.sql:1-12`
  - `services/promotions_service.py:101-113`
  - `services/promotions_service.py:201-210`
- Shopify Admin GraphQL `discountNodes` reads require `read_discounts`; the current merchant token now has `read_discounts`, `write_discounts`, and `read_customers`, and the internal preflight confirms those scopes before sync/fixture operations:
  - `config/settings.py:70-77`
  - `services/shopify_integration_verify.py:24-35`
  - `routes/merchant_store_connections.py:789-798`
  - `routes/shopify_promotions_sync_api.py:109-153`

### 2. Where eligibility is computed

- Shopify-native eligibility at quote time primarily comes from Shopify Storefront `discountCodes { code applicable }` and line/cart `discountAllocations`:
  - `services/shopify_storefront_pricing_service.py:355-377`
  - `services/shopify_storefront_pricing_service.py:434-555`
  - `services/shopify_storefront_pricing_service.py:1420-1458`
  - `services/shopify_storefront_pricing_service.py:1744-1779`
- New-customer evidence is fetched from Shopify Admin GraphQL via `Customer.numberOfOrders`:
  - `services/shopify_storefront_pricing_service.py:746-808`
  - `services/shopify_storefront_pricing_service.py:1189-1200`
- Pivota manual promotions are gated on top of Shopify evidence. If Shopify already applied a discount and the promotion config does not explicitly allow stacking, Pivota skips the manual promo. New-customer manual promos are also skipped when Shopify evidence is missing or ineligible:
  - `services/quote_service.py:420-474`
  - `services/quote_service.py:476-496`
  - `services/quote_service.py:627-658`
- If Shopify rejects a submitted discount code and no Shopify discount is applied, Pivota manual promotions are skipped by default unless the promotion explicitly opts into `canApplyWhenShopifyCodeRejected=true`:
  - `services/quote_service.py:437-449`
  - `services/quote_service.py:641-658`

### 3. Where Shopify-native constructs are created

- Production quote/order paths remain read/sync only for merchant runtime discounts.
- For this audit only, there is now an admin-key-protected internal fixture creator that can create bounded Shopify-native validation discounts and segments:
  - `services/shopify_discount_fixture_service.py:1-375`
  - `routes/shopify_promotions_sync_api.py:33-267`
- Shopify order writeback does create order-level `discount_codes`, metadata annotations, and external PSP transactions on the Shopify order once payment is complete:
  - `routes/order_routes.py:1191-1267`
  - `routes/order_routes.py:2873-2903`

### 4. Where checkout/cart behavior is validated

- Unit coverage for Storefront discount parsing and shipping-discount netting:
  - `tests/test_shopify_storefront_discount_evidence.py:20-127`
  - `tests/test_shopify_storefront_discount_evidence.py:207-236`
- Unit coverage for GraphQL discount-node mapping:
  - `tests/test_shopify_promotions_graphql_sync.py:9-86`
- Unit coverage for manual stacking/new-customer gates:
  - `tests/test_quote_service_promotions_auto_sync.py:245-338`
- Unit coverage for rejected-code fallback isolation:
  - `tests/test_quote_service_promotions_auto_sync.py:245-348`
- Unit coverage for quote-to-order discount reconciliation helpers:
  - `tests/test_shopify_order_discount_reconciliation.py:27-118`
- Unit coverage for PSP status, amount, and currency verification:
  - `tests/test_order_payment_verification.py:45-156`
- Live quote/cart and order-create artifacts:
  - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/`
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/`
  - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/`
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/`
  - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/`

### 5. Where final pricing is compared or reconciled

- Quote snapshots carry pricing, promotion lines, and discount evidence into order metadata:
  - `routes/order_routes.py:1594-1624`
- Shopify order annotations and reconciliation compare:
  - Pivota quote total
  - Pivota quote discount total
  - Shopify order total
  - Shopify total discounts
  - Shopify transaction totals
  - `routes/order_routes.py:1136-1360`
  - `routes/order_routes.py:2914-2940`
- Payment confirmation now also verifies PSP status, amount, and currency before the paid transition:
  - `routes/order_routes.py:304-407`
  - `adapters/psp_adapter.py:288-361`

## What definitely works today

1. Shopify Storefront code acceptance/rejection is wired through into quote responses.
   - Code rows and applicability are extracted from Storefront cart data in `services/shopify_storefront_pricing_service.py:355-377`.
   - Invalid code handling is unit-tested in `tests/test_shopify_storefront_discount_evidence.py:100-126`.
   - Live evidence:
     - US `PIVOTA_TEST_AMOUNT10` applicable true, discount total `2.90`.
     - CA `PIVOTA_TEST_FREESHIP` applicable false, no discount.
     - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/quote-matrix/summary.json`
     - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/summary.csv`

2. Product-level discount allocations are parsed from Shopify line allocations and exposed as `promotion_lines`, `applications`, `line_discount_total`, and effective unit pricing.
   - `services/shopify_storefront_pricing_service.py:434-503`
   - `tests/test_shopify_storefront_discount_evidence.py:20-57`

3. Free-shipping now uses Shopify cart-level shipping discount allocations and nets shipping correctly.
   - `services/shopify_storefront_pricing_service.py:504-555`
   - `services/shopify_storefront_pricing_service.py:1216-1233`
   - `services/shopify_storefront_pricing_service.py:2196-2211`
   - `tests/test_shopify_storefront_discount_evidence.py:59-97`
   - `tests/test_shopify_storefront_discount_evidence.py:207-236`
   - Live evidence:
     - US free-shipping code turns gross `29.00` shipping into net `0.00`.
     - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/quote-matrix/summary.json`

4. BXGY quantity gating works for the tested merchant fixture.
   - Live evidence shows `PIVOTA_TEST_BXGY` rejected at quantity `2` and accepted at quantity `3`, discounting `29.00`:
   - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/summary.csv`
   - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`

5. Automatic, fixed-amount product, and fixed-amount order execution are all live-proven on the current merchant.
   - Automatic:
     - `Pivota Auto Test` applies with no code on the in-scope Krave product.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/krave_auto_no_code.response.json`
   - Fixed-amount product:
     - `PIVOTA_AUDIT_20260421C_FIXPROD60` returns `applicable=true`, `discount_total=0.60`, `line_discount_total=0.60`, and `unit_price_effective=1.09`.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/fixtures-20260421a/fixed_amount_product_scoped.response.json`
   - Fixed-amount order:
     - `PIVOTA_TEST_COMBO_B` is live-proven as a `$1.00` order discount and stacks positively with BXGY.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_plus_combo_b.response.json`

6. Basic non-stacking/conflict handling and positive combinability both work in the tested cases.
   - Shopify-native conflict behavior:
     - `PIVOTA_TEST_NOCOMBO_A + PIVOTA_TEST_BXGY` yields BXGY applicable and NOCOMBO_A rejected.
     - `PIVOTA_TEST_AMOUNT10 + PIVOTA_TEST_FREESHIP` yields amount-off applicable and free-shipping rejected.
     - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`
     - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/summary.csv`
   - Positive combinability:
     - `PIVOTA_TEST_BXGY + PIVOTA_TEST_COMBO_B` remains applied as `discount_class=product` plus `discount_class=order`, with total combined discount `10.29`.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_plus_combo_b.response.json`
   - Pivota manual promo guard:
     - `services/quote_service.py:437-474`
     - `services/quote_service.py:641-658`
     - `tests/test_quote_service_promotions_auto_sync.py:245-348`

7. Segment-restricted and new-customer-restricted Shopify-native discounts are both live-proven on the current merchant.
   - Segment-restricted:
     - `PIVOTA_AUDIT_20260421A_SEGMENT` returned `applicable=true`, synced `context=DiscountCustomerSegments`, and discounted the quote by `0.50`.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/fixtures-20260421a/segment_customer.response.json`
   - New customer:
     - `PIVOTA_AUDIT_20260421A_NEWCUST` returned `applicable=true`, with `customer_eligibility=verified/new_customer=true`, and discounted the quote by `0.75`.
     - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/fixtures-20260421a/new_customer.response.json`

8. Active-date scheduling is live-proven for before-start and in-window behavior.
   - `PIVOTA_AUDIT_20260421B_UPCOMING` is rejected before `startsAt` and accepted after `startsAt` with `discount_total=0.20`.
   - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/fixtures-20260421a/upcoming_b_prestart.response.json`
   - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/fixtures-20260421a/upcoming_b_active.response.json`

9. Usage-limit and per-customer enforcement are now live-proven end to end on the current merchant.
   - `PIVOTA_AUDIT_20260421A_LIMIT1` synced with `usageLimit=1` and `appliesOncePerCustomer=true`.
   - Repeated quote probes stayed `applicable=true`, which proved quote/cart alone does not consume the count.
   - Order `ORD_B27BD95A214B40D4` then redeemed the code successfully, and the post-redemption quote rerun returned `applicable=false`, `discount_total=0`, and `shopify_order_count=1`.
   - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/usage-limit-redemption/02b-order-paid-public-lookup.json`
   - `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/usage-limit-redemption/03-post-redemption-quote.response.json`

10. Quote-backed order creation preserves discounted totals before payment confirmation.
   - Live order-create probe succeeded on retry, created a Pivota order at `55.10`, left `payment_status=awaiting_payment`, did not create a Shopify order before payment confirmation, and was cancelled cleanly:
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/01-order-create.json:31-218`
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/90-order-get.json:1-44`
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/92-order-get-after-cancel.json:1-44`

11. Payment confirmation no longer marks unpaid PSP references as paid.
   - Production rejected an unpaid Stripe PaymentIntent with `409 PAYMENT_NOT_SUCCEEDED`, left the Pivota order in `awaiting_payment`, did not create a Shopify order, and allowed cancellation:
   - `routes/order_routes.py:304-407`
   - `routes/agent_api.py:8259-8339`
   - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/summary.json`

12. Rejected Shopify codes no longer trigger local Pivota manual promotion fallback.
   - `PIVOTA_TEST_NOCOMBO_A` alone at quantity `3` now records `shopify_code_rejected`, returns `discount_total=0`, and emits no applied promotion lines:
   - `services/quote_service.py:437-449`
   - `services/quote_service.py:641-658`
   - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`

13. The approved live paid test merchant can complete discounted Stripe payments and write Shopify orders for the proven classes.
   - Free shipping: `ORD_508D4460ACA8DE11`, total `1.69 EUR`, Shopify order `7531476451656`.
   - Amount-off: `ORD_E2CC099ACF7A88A7`, total `2.22 EUR`, Shopify order `7531537269064`.
   - BXGY: `ORD_F56E0A1E5DC79E82`, total `4.07 EUR`, Shopify order `7531638980936`.
   - Refund cleanup evidence:
   - `artifacts/shopify-discount-validation/live-test-order-refund-status-after-ledger-repair-20260415T135016Z/summary.json`

## What partially works

1. GraphQL discount-node sync is real but metadata-first.
   - Pivota reads and stores metadata about Shopify discounts; it does not create/update merchant discounts from this path:
     - `services/shopify_promotions_sync.py:339-358`
     - `services/shopify_promotions_sync.py:621-700`
   - Runtime pricing still relies on Shopify Storefront applicability and allocations, not locally replaying discount formulas.
   - The new internal fixture creator is audit-only and admin-key gated; it is not part of the merchant runtime lifecycle.

2. Order reconciliation logic exists, is unit-tested, and is live-proven only for the approved Stripe + Shopify test merchant.
   - `routes/order_routes.py:1136-1360`
   - `routes/order_routes.py:2873-2940`
   - `tests/test_shopify_order_discount_reconciliation.py:27-118`
   - Three paid discounted orders were completed for the approved test merchant. Broader readiness still requires fail-closed canaries per merchant/PSP configuration.

3. PSP amount/currency verification is implemented for Stripe and unit-tested; fail-closed mode now blocks status-only PSP adapters.
   - `adapters/psp_adapter.py:288-361`
   - `routes/order_routes.py:304-407`
   - `tests/test_order_payment_verification.py:45-156`
   - Non-Stripe PSP adapters need equivalent structured status details before they can be used in fail-closed discount pilots.

## What is missing

1. Pilot-grade fail-closed canary evidence for each merchant/PSP/store configuration. The approved Stripe + Shopify test path is proven; other configurations are not.

2. Adapter-complete PSP amount/currency details for every PSP used in merchant pilots. Fail-closed now blocks status-only adapters, but that means non-Stripe paid pilots are blocked until adapter details exist.

3. More load evidence for checkout reliability on order creation. One live order-create probe returned `503 TEMPORARY_UNAVAILABLE` before succeeding on retry, and the code now has replay/backoff handling that needs continued production observation:
   - `artifacts/shopify-discount-validation/live-order-create-probe-20260415T054802Z/02-order-create.json:30-50`
   - Replay/backoff paths:
   - `routes/agent_api.py:7468-7489`
   - `routes/agent_api.py:7730-7749`
   - `routes/agent_api.py:8176-8179`
   - `utils/transient_errors.py:12-38`

4. Refund and platform-webhook observability.
   - The live refund cleanup uncovered a Shopify webhook double-counting bug. It is fixed, but pilot rollout should alert on any `total_refunded > total` condition.

## Highest-risk failure points

1. Discounted paid reconciliation is not yet proven across merchant/PSP configurations.
   - The approved Stripe + Shopify test merchant passed paid live canaries, but each new merchant/PSP setup can still drift between displayed offer and final charged price.

2. Successful payment transition depends on PSP adapter quality.
   - Stripe now returns structured status, amount, and currency details. In fail-closed mode, status-only adapters are blocked rather than trusted.

3. Order-create and replay/backoff behavior still need pilot observation.
   - The system now retries cleanly, but there is still live history of transient `503 TEMPORARY_UNAVAILABLE` during order create.

4. Refund and webhook reconciliation remain a monitoring concern even after the fixes.
   - The previous double-counting defect is fixed, but pilot rollout should still alert on any `total_refunded > total` or duplicated Shopify refund observations.

## Recommended next fixes ranked by impact × implementation effort

1. Run the next paid canary with `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, then compare quote total, PSP amount, Shopify order total, Shopify total discounts, Shopify transactions, and refund status.
2. Keep the internal preflight validator in front of any future fixture or merchant setup change and treat any `fail` row as a rollout blocker.
3. Add amount/currency-verifying adapters for any non-Stripe PSP that will participate in fail-closed pilots.
4. Keep refund and order-create monitoring enabled during pilots, with explicit alerts for over-refund and replay/backoff anomalies.
