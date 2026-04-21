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

- fixed-amount discount execution is still not proven
- segment/new-customer restricted Shopify-native discount execution is still not proven
- usage-limit exhaustion and active-window positive boundaries are still not proven

## Executive conclusion

The Shopify discount integration is no longer just cosmetic. The quote-time path now has live evidence for Shopify-native code acceptance/rejection, product discount allocation parsing, paid shipping, free-shipping netting, BXGY quantity gating, and basic conflict handling. The current production build reads Shopify Storefront `discountCodes` and `discountAllocations`, carries normalized discount evidence into quote snapshots, suppresses overlapping Pivota manual promotions, and now prevents a rejected Shopify code from being silently replaced by a local manual promo. Evidence is in `services/shopify_storefront_pricing_service.py:355-623`, `services/quote_service.py:437-449`, `services/quote_service.py:641-658`, and the live artifacts under `artifacts/shopify-discount-validation/`.

The paid path is now live-proven for this explicitly approved Stripe + Shopify test merchant across three discounted orders: free shipping (`ORD_508D4460ACA8DE11`), amount-off (`ORD_E2CC099ACF7A88A7`), and BXGY (`ORD_F56E0A1E5DC79E82`). Those orders were refunded through the production app path. The refund cleanup exposed and fixed three real defects: agent refund idempotency, Stripe Checkout Session refund resolution, and Shopify refund-webhook double counting.

This is still not ready for a broad merchant rollout. Discount-node sync is blocked for the current merchant custom app connection because Shopify Admin GraphQL denied `discountNodes` without `read_discounts`; this merchant uses its own Shopify custom app secret, so the stored custom app Admin token must be regenerated/updated with `read_discounts` before node sync can run. Positive combinability and true usage exhaustion have been reported fixture-ready but still need recorded rerun evidence; automatic discounts, restricted customer/segment discounts, and active-window-positive behavior remain fixture-blocked.

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
- Shopify Admin GraphQL `discountNodes` reads require `read_discounts`; for this merchant that scope must be present on the merchant custom app Admin token stored in Pivota:
  - `config/settings.py:70-77`
  - `services/shopify_integration_verify.py:24-35`
  - `routes/merchant_store_connections.py:789-798`

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

- v1 discount sync is read/sync only. There is no repo evidence that Pivota creates or updates merchant Shopify discounts through GraphQL discount mutations in this path.
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

5. Basic non-stacking/conflict handling works in the tested cases.
   - Shopify-native conflict behavior:
     - `PIVOTA_TEST_NOCOMBO_A + PIVOTA_TEST_BXGY` yields BXGY applicable and NOCOMBO_A rejected.
     - `PIVOTA_TEST_AMOUNT10 + PIVOTA_TEST_FREESHIP` yields amount-off applicable and free-shipping rejected.
     - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`
     - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/summary.csv`
   - Pivota manual promo guard:
     - `services/quote_service.py:437-474`
     - `services/quote_service.py:641-658`
     - `tests/test_quote_service_promotions_auto_sync.py:245-348`

6. Quote-backed order creation preserves discounted totals before payment confirmation.
   - Live order-create probe succeeded on retry, created a Pivota order at `55.10`, left `payment_status=awaiting_payment`, did not create a Shopify order before payment confirmation, and was cancelled cleanly:
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/01-order-create.json:31-218`
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/90-order-get.json:1-44`
   - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/92-order-get-after-cancel.json:1-44`

7. Payment confirmation no longer marks unpaid PSP references as paid.
   - Production rejected an unpaid Stripe PaymentIntent with `409 PAYMENT_NOT_SUCCEEDED`, left the Pivota order in `awaiting_payment`, did not create a Shopify order, and allowed cancellation:
   - `routes/order_routes.py:304-407`
   - `routes/agent_api.py:8259-8339`
   - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/summary.json`

8. Rejected Shopify codes no longer trigger local Pivota manual promotion fallback.
   - `PIVOTA_TEST_NOCOMBO_A` alone at quantity `3` now records `shopify_code_rejected`, returns `discount_total=0`, and emits no applied promotion lines:
   - `services/quote_service.py:437-449`
   - `services/quote_service.py:641-658`
   - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`

9. The approved live paid test merchant can complete discounted Stripe payments and write Shopify orders for the proven classes.
   - Free shipping: `ORD_508D4460ACA8DE11`, total `1.69 EUR`, Shopify order `7531476451656`.
   - Amount-off: `ORD_E2CC099ACF7A88A7`, total `2.22 EUR`, Shopify order `7531537269064`.
   - BXGY: `ORD_F56E0A1E5DC79E82`, total `4.07 EUR`, Shopify order `7531638980936`.
   - Refund cleanup evidence:
   - `artifacts/shopify-discount-validation/live-test-order-refund-status-after-ledger-repair-20260415T135016Z/summary.json`

## What partially works

1. Automatic discounts are wired in code, but not live-proven for this merchant.
   - Storefront queries request automatic allocation titles:
     - `services/shopify_storefront_pricing_service.py:1426-1457`
     - `services/shopify_storefront_pricing_service.py:1749-1779`
   - GraphQL sync maps `DiscountAutomaticBasic`, `DiscountAutomaticBxgy`, `DiscountAutomaticFreeShipping`:
     - `services/shopify_promotions_sync.py:407-470`
   - Missing live automatic fixture.

2. GraphQL discount-node sync is real but metadata-only.
   - Pivota reads and stores metadata about Shopify discounts; it does not create/update merchant discounts from this path:
     - `services/shopify_promotions_sync.py:339-358`
     - `services/shopify_promotions_sync.py:621-700`
   - This is enough for visibility/governance, not enough to claim Pivota manages merchant discount lifecycle.
   - The current merchant custom app token is blocked until the stored Admin token includes `read_discounts` because live Admin GraphQL returned `ACCESS_DENIED` for `discountNodes`.

3. New-customer logic exists, but only partially proven.
   - Shopify is queried for `numberOfOrders` by email and returns verified/unverified evidence:
     - `services/shopify_storefront_pricing_service.py:746-808`
   - Manual Pivota new-customer promos are blocked when evidence is missing:
     - `services/quote_service.py:476-496`
     - `services/quote_service.py:644-658`
     - `tests/test_quote_service_promotions_auto_sync.py:299-338`
   - Latest live responses for the supplied email return verified Shopify customer evidence with `shopify_order_count=3` and `new_customer=false`, but no native Shopify restricted-discount fixture was provided to prove acceptance/rejection at checkout.

4. Order reconciliation logic exists, is unit-tested, and is live-proven only for the approved Stripe + Shopify test merchant.
   - `routes/order_routes.py:1136-1360`
   - `routes/order_routes.py:2873-2940`
   - `tests/test_shopify_order_discount_reconciliation.py:27-118`
   - Three paid discounted orders were completed for the approved test merchant. Broader readiness still requires fail-closed canaries per merchant/PSP configuration.

5. PSP amount/currency verification is implemented for Stripe and unit-tested; fail-closed mode now blocks status-only PSP adapters.
   - `adapters/psp_adapter.py:288-361`
   - `routes/order_routes.py:304-407`
   - `tests/test_order_payment_verification.py:45-156`
   - Non-Stripe PSP adapters need equivalent structured status details before they can be used in fail-closed discount pilots.

6. Active date and usage-limit behavior are partially proven.
   - Live evidence shows expired codes are rejected:
     - `artifacts/shopify-discount-validation/live-us-discount-matrix-20260415T052251Z/summary.json:147-284`
   - Sync preserves `usageLimit`, `appliesOncePerCustomer`, and open-ended `endsAt=None`:
     - `services/shopify_promotions_sync.py:318-358`
     - `tests/test_shopify_promotions_graphql_sync.py:9-44`
   - The latest completed readonly matrix showed `PIVOTA_TEST_EXHAUSTED` applicable with a `0.90` discount, so it did not prove exhaustion. The merchant has since reported that the fixture is ready and needs rerun evidence.

## What is missing

1. Merchant custom app Admin token update/regeneration with `read_discounts`, then live discount-node sync proof for usage limits, active windows, combinations, and customer context.

2. A live automatic-discount fixture for this merchant.

3. A live new-customer-only or segment-restricted Shopify-native discount fixture.

4. Rerun the repaired live positive combinable pair fixture. Conflict handling is proven; positive stacking is pending rerun evidence.

5. Hard evidence for fixed-amount discount formulas. The validated live product-discount fixture behaves as percentage off, not fixed amount.

6. Pilot-grade fail-closed canary evidence for each merchant/PSP/store configuration. The approved Stripe + Shopify test path is proven; other configurations are not.

7. Adapter-complete PSP amount/currency details for every PSP used in merchant pilots. Fail-closed now blocks status-only adapters, but that means non-Stripe paid pilots are blocked until adapter details exist.

8. More load evidence for checkout reliability on order creation. One live order-create probe returned `503 TEMPORARY_UNAVAILABLE` before succeeding on retry, and the code now has replay/backoff handling that needs continued production observation:
   - `artifacts/shopify-discount-validation/live-order-create-probe-20260415T054802Z/02-order-create.json:30-50`
   - Replay/backoff paths:
   - `routes/agent_api.py:7468-7489`
   - `routes/agent_api.py:7730-7749`
   - `routes/agent_api.py:8176-8179`
   - `utils/transient_errors.py:12-38`

9. Refund and platform-webhook observability.
   - The live refund cleanup uncovered a Shopify webhook double-counting bug. It is fixed, but pilot rollout should alert on any `total_refunded > total` condition.

## Highest-risk failure points

1. Discounted paid reconciliation is not yet proven across merchant/PSP configurations.
   - The approved Stripe + Shopify test merchant passed paid live canaries, but each new merchant/PSP setup can still drift between displayed offer and final charged price.

2. Successful payment transition depends on PSP adapter quality.
   - Stripe now returns structured status, amount, and currency details. In fail-closed mode, status-only adapters are blocked rather than trusted.

3. Customer restrictions still depend on Shopify lookup quality and fixture availability.
   - Segment/specific-customer logic is stored as metadata, but not fully live-validated.

4. Positive discount stacking policy is under-proven.
   - Non-combinable rejection is proven; combinable acceptance is not.

5. Fixed-amount product/order discount behavior is not yet proven with live fixtures.

## Recommended next fixes ranked by impact × implementation effort

1. Update/regenerate the test merchant custom app Admin token with `read_discounts`, rerun GraphQL discount-node sync, and verify node metadata for usage limits, active dates, combinations, and customer context.
2. Run the next paid canary with `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, then compare quote total, PSP amount, Shopify order total, Shopify total discounts, Shopify transactions, and refund status.
3. Rerun the repaired `PIVOTA_TEST_COMBO_A`, then validate one positive combinable pair and one non-combinable pair against the same product/address.
4. Add Shopify automatic, restricted-customer/new-customer, active-window-positive, and true exhausted-usage fixtures for live validation.
5. Add one true fixed-amount product discount fixture and one true fixed-amount order discount fixture, then validate both quote-time and paid-order behavior.
