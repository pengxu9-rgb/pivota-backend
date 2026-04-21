# Shopify Discount Gap Analysis

## Update: 2026-04-21 current merchant rerun

The previous blockers `read_discounts missing`, `automatic not live-proven`, and `positive combinable pair pending rerun` are no longer true for the current merchant `merch_efbc46b4619cfbdf`.

New current-merchant state:

- GraphQL `discountNodes` sync is live and updated `6` discounts successfully.
- Automatic discount execution is live-proven (`Pivota Auto Test`).
- Positive combinability is live-proven for `PIVOTA_TEST_BXGY + PIVOTA_TEST_COMBO_B`.
- Storefront parser now normalizes cart-level order code allocations as `discount_class=order`.
- Quote and PDP/product-detail store-discount aggregates are deduped, so multi-item carts and multi-variant PDPs no longer duplicate store-offer rows.

Top remaining gaps for the current merchant now are:

1. Fixed-amount discount execution is still not proven. The validated `PIVOTA_TEST_AMOUNT10` fixture is percentage-off, not amount-off.
2. Segment-restricted and new-customer-restricted Shopify-native discount execution is still not live-proven.
3. Usage-limit exhaustion and active-window positive boundaries are still not live-proven.
4. Pilot-grade paid canaries under `fail_closed` reconciliation still need to be rerun for this merchant after the latest discount repairs.

## Top remaining gaps

1. `Pilot-grade reconciliation and refund monitoring` is still thin.
   - Three approved live paid discounted orders completed and were refunded, but the cleanup exposed real refund-path defects: agent refund idempotency, Stripe Checkout Session refund resolution, and Shopify refund-webhook double counting. Those are fixed, and `scripts/check_discount_order_canaries.py` now provides a read-only audit for missing Shopify links, non-authoritative pricing evidence, over-refunds, and Shopify webhook double-counting. Pilot rollout still needs this audit run after each paid canary plus fail-closed reconciliation alerts.

2. `GraphQL discount-node sync is blocked on the merchant custom app token scope`.
   - Shopify Admin GraphQL returned `ACCESS_DENIED` for `discountNodes` because the stored merchant custom app Admin token lacks `read_discounts`. This merchant connects through its own Shopify custom app secret, so the required action is to enable `read_discounts` on that custom app, regenerate/update the Admin API access token, update the stored Shopify credential in Pivota, and rerun the preflight.

3. `Positive combinable discount coverage` is pending rerun after merchant fixture repair.
   - The last completed validation had `PIVOTA_TEST_COMBO_A` returning `applicable=false`, so the system still lacks recorded proof that two intended combinable Shopify-native discounts can both apply and remain aligned with Pivota quote totals. The merchant has since reported that the fixture is repaired; it must be rerun before this can be marked proven.

4. `Automatic and restricted-customer discount execution` is not live-proven.
   - Storefront parsing, GraphQL sync metadata, and new-customer evidence logic exist, but this merchant still needs explicit Shopify-native fixtures for automatic, segment, and new-customer cases.

5. `Usage-limit and active-window positive boundaries are pending rerun or still fixture-blocked`.
   - The last completed validation had `PIVOTA_TEST_EXHAUSTED` still applicable in quote/cart validation, and quote probes do not consume usage. The merchant has since reported that the exhausted fixture is ready; it must be rerun before usage exhaustion can be marked proven. `PIVOTA_TEST_EXPIRED` proves inactive rejection, but there is no active-window-positive fixture.

6. `PSP amount/currency verification is Stripe-proven, not adapter-complete`.
   - Stripe PaymentIntent and Checkout Session structured status parsing is implemented and unit-tested. Fail-closed mode now blocks status-only PSP adapters, so other PSPs need equivalent structured amount/currency verification before paid discount pilots.

7. `Merchant setup validation now has a preflight gate, but it must be run after fixture changes`.
   - `scripts/preflight_shopify_discounts.py` checks backend health, product/variant quoteability, authoritative shipping evidence, fixture code behavior, and read-only Admin GraphQL `discountNodes` access when an internal admin key is provided. Remaining blockers are merchant-side: update the merchant custom app Admin token for `read_discounts`, rerun the repaired combinable/exhausted fixtures, and add automatic/customer-context fixtures.

## What should be fixed first

1. Update/regenerate the test merchant custom app Admin token after enabling `read_discounts`, update the stored Shopify credential in Pivota, rerun discount-node preflight/sync, and verify combinations, usage limits, active dates, and customer context from Admin GraphQL.
2. Run the next paid canary with `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, then verify quote total, PSP amount/currency, Shopify order total, Shopify total discounts, Shopify transactions, and refund status.
3. Rerun the repaired `PIVOTA_TEST_COMBO_A` fixture so Storefront returns `applicable=true` together with its intended companion code, then record the combinability matrix evidence.
4. Add merchant fixtures for automatic discount, new-customer/segment eligibility, active scheduling window, and available-then-exhausted usage limit.
5. Run the preflight validator after each merchant-side fixture or scope change and treat any `fail` row as a blocker before paid canaries.

## Rollout position

- Quote/cart discount pilots can proceed only for allowlisted merchants and only for the proven classes: percentage product discount, BXGY code, free-shipping code, invalid-code rejection, expired code rejection, and non-combinable conflict handling.
- Paid checkout pilots can proceed only as controlled canaries for the exact proven Stripe + Shopify setup, with `fail_closed` reconciliation and refund monitoring enabled.
- Any pilot using non-Stripe PSPs is blocked in fail-closed mode unless the adapter returns verified status, amount, and currency details.
