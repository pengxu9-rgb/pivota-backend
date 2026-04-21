# Shopify Discount Gap Analysis

## Update: 2026-04-21 current merchant rerun

The previous blockers `read_discounts missing`, `automatic not live-proven`, `positive combinable pair pending rerun`, `segment/new-customer fixture missing`, and `active-window positive missing` are no longer true for the current merchant `merch_efbc46b4619cfbdf`.

New current-merchant state:

- GraphQL `discountNodes` sync is live and current-merchant reruns now include write-backed validation fixtures.
- Automatic discount execution is live-proven (`Pivota Auto Test`).
- Positive combinability is live-proven for `PIVOTA_TEST_BXGY + PIVOTA_TEST_COMBO_B`.
- Product-scoped fixed-amount code execution is live-proven via `PIVOTA_AUDIT_20260421C_FIXPROD60`.
- Segment-restricted execution is live-proven via `PIVOTA_AUDIT_20260421A_SEGMENT`.
- New-customer execution is live-proven via `PIVOTA_AUDIT_20260421A_NEWCUST`.
- Active-window before/during behavior is live-proven via `PIVOTA_AUDIT_20260421B_UPCOMING`.
- Storefront parser now normalizes cart-level order code allocations as `discount_class=order`.
- Quote and PDP/product-detail store-discount aggregates are deduped, so multi-item carts and multi-variant PDPs no longer duplicate store-offer rows.

Top remaining gaps for the current merchant now are:

1. Usage-limit exhaustion is still not live-proven because quote/cart probes do not consume Shopify usage count.
2. Pilot-grade paid canaries under `fail_closed` reconciliation still need to be rerun for this merchant after the latest discount repairs.

## Top remaining gaps

1. `Pilot-grade reconciliation and refund monitoring` is still thin.
   - Three approved live paid discounted orders completed and were refunded, but the cleanup exposed real refund-path defects: agent refund idempotency, Stripe Checkout Session refund resolution, and Shopify refund-webhook double counting. Those are fixed, and `scripts/check_discount_order_canaries.py` now provides a read-only audit for missing Shopify links, non-authoritative pricing evidence, over-refunds, and Shopify webhook double-counting. Pilot rollout still needs this audit run after each paid canary plus fail-closed reconciliation alerts.

2. `Usage-limit exhausted boundary` is still open.
   - `PIVOTA_AUDIT_20260421A_LIMIT1` now proves synced `usageLimit=1` plus `appliesOncePerCustomer=true`, and repeated quote probes remained `applicable=true`.
   - That is expected: quote/cart validation does not consume Shopify usage count. A controlled redeemed order and a post-redemption rejection probe are still required.

3. `PSP amount/currency verification is Stripe-proven, not adapter-complete`.
   - Stripe PaymentIntent and Checkout Session structured status parsing is implemented and unit-tested. Fail-closed mode now blocks status-only PSP adapters, so other PSPs need equivalent structured amount/currency verification before paid discount pilots.

4. `Merchant setup validation now has a preflight gate and bounded fixture creator`.
   - `scripts/preflight_shopify_discounts.py` checks backend health, product/variant quoteability, authoritative shipping evidence, fixture code behavior, and read-only Admin GraphQL `discountNodes` access when an internal admin key is provided.
   - `routes/shopify_promotions_sync_api.py` plus `services/shopify_discount_fixture_service.py` now create bounded Shopify-native validation fixtures for this audit under admin-key control.

## What should be fixed first

1. Run one controlled redeemed order with `PIVOTA_AUDIT_20260421A_LIMIT1`, then rerun the same quote probe to capture the exhausted rejection boundary.
2. Run the next paid canary with `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, then verify quote total, PSP amount/currency, Shopify order total, Shopify total discounts, Shopify transactions, and refund status.
3. Keep the preflight validator in front of any future merchant-side fixture change and treat any `fail` row as a blocker before paid canaries.
4. If multi-PSP pilots are needed, add amount/currency-verifying adapters for non-Stripe PSPs before enabling fail-closed paid rollouts there.

## Rollout position

- Quote/cart discount pilots can proceed only for allowlisted merchants and the now-proven classes: percentage product discount, fixed-amount product discount, fixed-amount order discount, BXGY code, free-shipping code, segment/new-customer code restrictions, invalid-code rejection, expired code rejection, active-window before/during behavior, and non-combinable conflict handling.
- Paid checkout pilots can proceed only as controlled canaries for the exact proven Stripe + Shopify setup, with `fail_closed` reconciliation and refund monitoring enabled.
- Any pilot using non-Stripe PSPs is blocked in fail-closed mode unless the adapter returns verified status, amount, and currency details.
