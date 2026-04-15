# Shopify Discount Gap Analysis

## Top remaining gaps

1. `Successful paid reconciliation` is still not live-proven.
   - The quote, order, and payment-confirm guard paths are materially safer now, but no successful discounted payment has created and reconciled a Shopify order in this validation cycle.

2. `Positive combinable discount coverage` is fixture-blocked.
   - `PIVOTA_TEST_COMBO_A` returned `applicable=false`, so the system still lacks proof that two intended combinable Shopify-native discounts can both apply and remain aligned with Pivota quote totals.

3. `Automatic and restricted-customer discount execution` is not live-proven.
   - Storefront parsing, GraphQL sync metadata, and new-customer evidence logic exist, but this merchant still needs explicit Shopify-native fixtures for automatic, segment, and new-customer cases.

4. `PSP amount/currency verification is Stripe-proven, not adapter-complete`.
   - Stripe PaymentIntent and Checkout Session structured status parsing is implemented and unit-tested. Fail-closed mode now blocks status-only PSP adapters, so other PSPs need equivalent structured amount/currency verification before paid discount pilots.

5. `Merchant setup validation is still manual`.
   - Shipping zones, markets, delivery rates, discount scope, usage limits, active windows, and combination settings are still validated by ad hoc probes rather than a preflight validator.

## What should be fixed first

1. Execute one successful discounted test payment with reconciliation in `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`, using a non-production test store or explicitly approved test merchant setup.
2. Replace or repair `PIVOTA_TEST_COMBO_A` so Storefront returns `applicable=true` together with its intended companion code, then rerun the combinability matrix.
3. Add merchant fixtures for automatic discount, new-customer/segment eligibility, active scheduling window, and available-then-exhausted usage limit.
4. Add structured `get_payment_status_details` amount/currency support for every non-Stripe PSP adapter that should be allowed in discount pilots; unsupported adapters are now blocked in fail-closed mode.
5. Add a merchant preflight validator that checks Shopify app scopes, Markets, shipping delivery options, test product/variant availability, discount code eligibility, combination flags, usage limits, and active dates before live validation begins.

## Rollout position

- Quote/cart discount pilots can proceed only for allowlisted merchants and only for the proven classes: percentage product discount, BXGY code, free-shipping code, invalid-code rejection, exhausted/expired code rejection, and non-combinable conflict handling.
- Paid checkout pilots should not proceed until successful paid reconciliation passes at least once with the exact PSP and Shopify store configuration being piloted.
- Any pilot using non-Stripe PSPs is blocked in fail-closed mode unless the adapter returns verified status, amount, and currency details.
