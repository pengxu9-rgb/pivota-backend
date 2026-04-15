# Shopify Discount Gap Analysis

## Top remaining gaps

1. `Pilot-grade reconciliation and refund monitoring` is still thin.
   - Three approved live paid discounted orders completed and were refunded, but the cleanup exposed real refund-path defects: agent refund idempotency, Stripe Checkout Session refund resolution, and Shopify refund-webhook double counting. Those are fixed, but pilot rollout needs canary monitoring and fail-closed reconciliation alerts.

2. `GraphQL discount-node sync is blocked on merchant reauthorization`.
   - Shopify Admin GraphQL returned `ACCESS_DENIED` for `discountNodes` because the installed token lacks `read_discounts`. Production `SHOPIFY_SCOPES` now requests `read_discounts`, but this merchant must reconnect before node sync can read usage limits, combinations, context, and active windows.

3. `Positive combinable discount coverage` is fixture-blocked.
   - `PIVOTA_TEST_COMBO_A` returned `applicable=false`, so the system still lacks proof that two intended combinable Shopify-native discounts can both apply and remain aligned with Pivota quote totals.

4. `Automatic and restricted-customer discount execution` is not live-proven.
   - Storefront parsing, GraphQL sync metadata, and new-customer evidence logic exist, but this merchant still needs explicit Shopify-native fixtures for automatic, segment, and new-customer cases.

5. `Usage-limit and active-window positive boundaries are fixture-blocked`.
   - `PIVOTA_TEST_EXHAUSTED` is currently still applicable in quote/cart validation, and quote probes do not consume usage. `PIVOTA_TEST_EXPIRED` proves inactive rejection, but there is no active-window-positive fixture.

6. `PSP amount/currency verification is Stripe-proven, not adapter-complete`.
   - Stripe PaymentIntent and Checkout Session structured status parsing is implemented and unit-tested. Fail-closed mode now blocks status-only PSP adapters, so other PSPs need equivalent structured amount/currency verification before paid discount pilots.

7. `Merchant setup validation is still manual`.
   - Shipping zones, markets, delivery rates, discount scope, usage limits, active windows, and combination settings are still validated by ad hoc probes rather than a preflight validator.

## What should be fixed first

1. Reconnect the test merchant Shopify app after `read_discounts` is requested, rerun discount-node sync, and verify combinations, usage limits, active dates, and customer context from Admin GraphQL.
2. Run the next paid canary with `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, then verify quote total, PSP amount/currency, Shopify order total, Shopify total discounts, Shopify transactions, and refund status.
3. Replace or repair `PIVOTA_TEST_COMBO_A` so Storefront returns `applicable=true` together with its intended companion code, then rerun the combinability matrix.
4. Add merchant fixtures for automatic discount, new-customer/segment eligibility, active scheduling window, and available-then-exhausted usage limit.
5. Add a merchant preflight validator that checks Shopify app scopes, Markets, shipping delivery options, test product/variant availability, discount code eligibility, combination flags, usage limits, and active dates before live validation begins.

## Rollout position

- Quote/cart discount pilots can proceed only for allowlisted merchants and only for the proven classes: percentage product discount, BXGY code, free-shipping code, invalid-code rejection, expired code rejection, and non-combinable conflict handling.
- Paid checkout pilots can proceed only as controlled canaries for the exact proven Stripe + Shopify setup, with `fail_closed` reconciliation and refund monitoring enabled.
- Any pilot using non-Stripe PSPs is blocked in fail-closed mode unless the adapter returns verified status, amount, and currency details.
