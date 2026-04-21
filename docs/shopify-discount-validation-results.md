# Shopify Discount Validation Results

## Update: 2026-04-21 current merchant rerun

Current merchant: `merch_efbc46b4619cfbdf`

This update supersedes the older `read_discounts blocked / automatic fixture missing / positive combinability pending` status for the current merchant.

Passed on 2026-04-21:

- Admin discount-node sync is live for the current merchant. Internal sync updated `6` discount nodes successfully via `discountNodes`.
- `PIVOTA_TEST_AMOUNT10` now has synced product scope metadata for the correct products and applies cleanly on its in-scope Winona quote.
- `PIVOTA_TEST_BXGY` is live-proven on the correct buy/get cart:
  - buy product `10064572285225` variant `53012705509673` qty `2`
  - get product `10064567370025` variant `53012684341545` qty `1`
  - `discount_evidence.codes[0].applicable=true`
  - Shopify allocation applied `-9.29` to the get-item line
- `PIVOTA_TEST_BXGY + PIVOTA_TEST_COMBO_B` is live-proven as a positive combinable pair for the current merchant:
  - BXGY remains applied as `discount_class=product`
  - `PIVOTA_TEST_COMBO_B` remains applied as `discount_class=order`
  - total combined discount `10.29`
- Automatic discount is now live-proven for the current merchant:
  - `Pivota Auto Test` applies automatically on `10064558096681` with no code
  - discount amount `5.60`
- Automatic-vs-code conflict is live-proven:
  - on the same product, submitted code `PIVOTA_TEST_AMOUNT10` returns `applicable=false`
  - automatic `Pivota Auto Test` remains the only applied discount
- Quote `store_discount_evidence.offers` is now deduped across multi-item carts.
- Product-detail `store_discount_evidence.offers` and `decisions` are now deduped across variants.

Still not proven for the current merchant:

- fixed-amount product/order discount execution
- segment-restricted or new-customer-restricted Shopify-native discount execution
- usage-limit exhaustion boundary
- active-window positive boundary

Current evidence artifacts:

- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_real_target.request.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_real_target.response.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_plus_combo_b.request.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/bxgy_plus_combo_b.response.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/krave_auto_no_code.request.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/krave_auto_no_code.response.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/krave_auto_plus_amount10.request.json`
- `/Users/pengchydan/dev/reports/shopify-discount-validation/20260421-live-legacy-guard/krave_auto_plus_amount10.response.json`

## Tested scenarios

Validation date: 2026-04-15.

Validation scope included production quote/cart probes, quote-backed order creation without completing payment, unpaid payment-confirmation rejection, focused post-deploy regression probes, three explicitly approved live paid discounted orders, and refund cleanup through the production app path.

- US baseline quote with no discount code
- US amount-off code
- US free-shipping code
- CA baseline quote with no discount code
- CA free-shipping code against non-US address
- CA amount-off code against non-US address
- BXGY boundary at quantity 2
- BXGY positive case at quantity 3
- non-combinable pair with BXGY at quantity 3
- expired code
- usage-limit fixture probe
- cross-class stacking probe with amount-off plus free shipping
- positive combinable fixture probe with `PIVOTA_TEST_COMBO_A`
- quote -> order create probe with amount-off quote, without payment confirmation
- unpaid payment confirmation guard
- live paid free-shipping order
- live paid amount-off order
- live paid BXGY order
- live app-path refund cleanup for the three paid test orders
- rejected Shopify code against quantity-based Pivota manual fallback
- PSP amount/currency verification unit coverage

## Passed

- `US baseline`: shipping is priced and returned as `29.00`; pricing confidence is authoritative.
- `US amount-off`: `PIVOTA_TEST_AMOUNT10` is applicable, applies `2.90` off the line item, and keeps shipping at `29.00`.
- `US free shipping`: `PIVOTA_TEST_FREESHIP` is applicable, gross shipping is `29.00`, Shopify shipping discount nets `shipping_fee` to `0.00`, and final total is `29.00`.
- `CA baseline`: shipping is priced at `29.00`; pricing confidence is authoritative.
- `CA free shipping negative`: `PIVOTA_TEST_FREESHIP` is rejected with `applicable=false` for a Canada address, and shipping remains `29.00`.
- `CA amount-off`: `PIVOTA_TEST_AMOUNT10` remains applicable for the Canada address and does not zero out shipping.
- `BXGY boundary`: `PIVOTA_TEST_BXGY` is not applicable at quantity `2`, which is consistent with a buy-2-get-1 style threshold.
- `BXGY positive`: `PIVOTA_TEST_BXGY` becomes applicable at quantity `3` and applies `29.00` off.
- `Non-combinable pair`: with `PIVOTA_TEST_NOCOMBO_A + PIVOTA_TEST_BXGY` at quantity `3`, Shopify accepts `PIVOTA_TEST_BXGY` and rejects `PIVOTA_TEST_NOCOMBO_A` cleanly.
- `Expired code`: `PIVOTA_TEST_EXPIRED` is rejected with `applicable=false`.
- `Cross-class stacking probe`: `PIVOTA_TEST_AMOUNT10 + PIVOTA_TEST_FREESHIP` does not stack in the tested configuration; Shopify accepts the amount-off code and rejects the free-shipping code cleanly.
- `Order create probe`: a quote-backed amount-off order created successfully on retry, preserved the discounted total `55.10`, stayed in `awaiting_payment`, did not create a Shopify order before payment confirmation, and was then cancelled cleanly.
- `Unpaid payment confirmation guard`: production rejected an unpaid Stripe PaymentIntent with `409 PAYMENT_NOT_SUCCEEDED`, left the order in `awaiting_payment`, kept `shopify_order_id=null`, and allowed cancellation.
- `PSP amount/currency guard`: automated tests now prove successful PSP status is not enough; amount and currency must also match the Pivota order before an order can be marked paid, and fail-closed mode rejects status-only PSP adapters.
- `Rejected-code fallback isolation`: production no longer lets an inapplicable Shopify code trigger a Pivota quantity-based manual promotion. `PIVOTA_TEST_NOCOMBO_A` alone at quantity `3` now returns `discount_total=0`, records a skipped decision reason `shopify_code_rejected`, and emits no applied promotion lines.
- `Live paid free shipping`: `ORD_508D4460ACA8DE11` completed at `1.69 EUR`, wrote back to Shopify order `7531476451656`, and was later refunded through the production app route.
- `Live paid amount-off`: `ORD_E2CC099ACF7A88A7` completed at `2.22 EUR`, wrote back to Shopify order `7531537269064`, and was later refunded through the production app route.
- `Live paid BXGY`: `ORD_F56E0A1E5DC79E82` completed at `4.07 EUR`, wrote back to Shopify order `7531638980936`, and was later refunded through the production app route.
- `Refund cleanup after fixes`: production `/orders/{order_id}/refund-status` now reports `total_refunded == original_amount` for all three live test orders after fixing Stripe Checkout Session refund resolution and Shopify refund-webhook double counting.

## Failed

- `PIVOTA_TEST_COMBO_A` positive combinability fixture failed the last completed validation. Shopify returned `applicable=false` for `PIVOTA_TEST_COMBO_A`; with `PIVOTA_TEST_AMOUNT10 + PIVOTA_TEST_COMBO_A`, only `PIVOTA_TEST_AMOUNT10` applied. The merchant has since reported that this fixture is repaired; it is pending rerun evidence.
- `PIVOTA_TEST_EXHAUSTED` did not prove exhaustion in the last completed validation. The latest readonly matrix returned `applicable=true` and `discount_total=0.90`. The merchant has since reported that this fixture is ready; it is pending rerun evidence.

## Blocked

- `automatic discount live execution`: blocked by missing explicit automatic-discount fixture for this merchant.
- `segment-restricted / new-customer restricted discount live execution`: the lookup path is implemented, but no restricted Shopify-native fixture code was supplied for checkout validation.
- `positive combinable pair`: pending rerun after the merchant fixture repair; Shopify must mark both intended codes applicable together before this becomes rollout evidence.
- `available usage-limit boundary`: pending rerun after the merchant fixture repair. Quote probes do not consume usage, so a separate available-then-exhausted fixture or controlled paid-consumption test is still needed for the full boundary.
- `active window positive case`: no active scheduling-window fixture was supplied; expired/inactive rejection was proven.
- `GraphQL discount-node sync for the current merchant custom app connection`: Shopify Admin GraphQL returned `ACCESS_DENIED` for `discountNodes` because the stored custom app Admin token lacks `read_discounts`. Enable `read_discounts` on the merchant custom app, regenerate/update the Admin API access token, update the stored Shopify credential in Pivota, then rerun preflight/sync.

## Root cause by failure

- Historical free-shipping failure was caused by Pivota reading gross delivery price without netting Shopify shipping discount allocations into `shipping_fee`. That defect is fixed and live evidence now shows US free-shipping and CA paid-shipping behavior diverge correctly.
- The positive combinability failure is fixture-side unless the business expected `PIVOTA_TEST_COMBO_A` to be eligible for the test product/address. Storefront returned `applicable=false`, so Pivota should not apply or simulate that discount.
- The rejected-code fallback defect was Pivota-side: a rejected Shopify code could previously fall through into local infra promotions. The fix now records the skipped manual promotion as a decision instead of applying it.
- The first app-path refund attempt exposed an agent refund proxy bug: the internal request object did not carry `idempotency_key`. PR #179 fixed that path.
- The second refund attempt exposed a Stripe bug: Pivota stores Stripe Checkout Session IDs (`cs_...`) for hosted Checkout, while the refund adapter tried to refund them as PaymentIntent IDs. PR #180 now resolves Checkout Sessions to PaymentIntents before creating refunds.
- The successful refunds exposed a Shopify webhook reconciliation bug: Pivota-originated Shopify refund/cancel writeback triggered Shopify `refunds/create`, and the platform refund webhook handler counted that event as a second monetary refund. PR #181 now treats Shopify refund webhooks as observation-only for external PSP orders and ignores over-refund events.

## Exact evidence references

- Final US/CA shipping matrix after deployment:
  - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/quote-matrix/summary.json`
  - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/quote-matrix/US-FREESHIP.json`
  - `artifacts/shopify-discount-validation/live-us-ca-shipping-matrix-20260415T075522Z/quote-matrix/CA-FREESHIP.json`
- Discount matrix after PSP guard deployment:
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/summary.csv`
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/SFD-001.json`
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/SFD-004.json`
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/SFD-005.json`
  - `artifacts/shopify-discount-validation/live-us-discount-matrix-post-psp-guard-20260415T075604Z/SFD-010.json`
- Unpaid PSP confirmation and cancellation guard:
  - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/summary.json`
  - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/20-confirm-payment.json`
  - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/21-order-after-confirm-reject.json`
  - `artifacts/shopify-discount-validation/live-psp-amount-currency-guard-20260415T075403Z/manual-probes/31-order-after-cancel.json`
- Rejected-code fallback isolation:
  - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/summary.json`
  - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/NOCOMBO-A-ALONE-Q3.json`
  - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/BXGY-ALONE-Q3.json`
  - `artifacts/shopify-discount-validation/live-rejected-code-promo-skip-20260415T080245Z/quote-matrix/NOCOMBO-A-BXGY-Q3.json`
- Order create probe before payment confirmation:
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/summary.json`
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/01-order-create.json`
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/90-order-get.json`
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/91-order-cancel.json`
  - `artifacts/shopify-discount-validation/live-order-create-retry-20260415T054846Z/92-order-get-after-cancel.json`
- Latest readonly matrix after paid/refund fixes:
  - `artifacts/shopify-discount-validation/live-post-read-discounts-scope-readonly-20260415T140417Z/quote-matrix/summary.csv`
  - `artifacts/shopify-discount-validation/live-post-read-discounts-scope-readonly-20260415T140417Z/quote-matrix/SFD-007.json`
  - `artifacts/shopify-discount-validation/live-post-read-discounts-scope-readonly-20260415T140417Z/quote-matrix/SFD-010.json`
  - `artifacts/shopify-discount-validation/live-post-refund-fixes-readonly-20260415T135141Z/quote-matrix/summary.csv`
  - `artifacts/shopify-discount-validation/live-post-refund-fixes-readonly-20260415T135141Z/quote-matrix/SFD-007.json`
  - `artifacts/shopify-discount-validation/live-post-refund-fixes-readonly-20260415T135141Z/quote-matrix/SFD-010.json`
- Live refund cleanup and ledger repair:
  - `artifacts/shopify-discount-validation/live-test-order-refunds-admin-after-checkout-session-fix-20260415T133853Z/summary.json`
  - `artifacts/shopify-discount-validation/live-test-order-refund-ledger-repair-20260415T134952Z/refund-ledger-repair.json`
  - `artifacts/shopify-discount-validation/live-test-order-refund-status-after-ledger-repair-20260415T135016Z/summary.json`

## Whether the system is ready for merchant pilots on discounts

`limited`

Rationale:

- Quote-time Shopify-native amount-off, free-shipping, BXGY threshold behavior, invalid-code rejection, expired rejection, and non-combinable conflict behavior now have production evidence.
- Authoritative Storefront delivery pricing is now carried into quotes; US no-code and CA no-code both price shipping at `29.00`, while the US free-shipping code nets shipping to `0.00`.
- The most dangerous fake-discount path found in this round is fixed: rejected Shopify codes no longer trigger Pivota manual fallback promotions.
- Payment confirmation now refuses unpaid PSP references and unit tests enforce amount/currency matching before paid transition; fail-closed mode blocks PSP adapters that cannot provide amount/currency details.
- Three live paid discounted orders completed and were refunded, but broad rollout is still blocked by fixture gaps, missing `read_discounts` on the merchant custom app Admin token for discount-node sync, and the need to run merchant-pilot canaries in `fail_closed` reconciliation mode with alerting.
