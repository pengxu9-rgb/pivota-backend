# Shopify Discount Validation Gate

Use this gate before merging Shopify discount execution changes. It must target
a dev/test Pivota merchant by default.

Live validation is allowed only when explicitly requested and only in
`live_no_order` mode. That mode may exercise live quote/cart paths and live
discount codes, but it must not create Shopify orders, confirm PSP payments, or
create/update Shopify discount nodes. Treat Storefront cart validation as live
cart traffic, not a purely read-only operation.

## Required fixtures

Configure these as GitHub Actions secrets or variables before running the
manual workflow:

| Name | Secret or variable | Notes |
| --- | --- | --- |
| `SHOPIFY_DISCOUNT_TEST_BASE_URL` | secret or variable | Pivota backend URL. Use dev/test by default; live no-order validation requires explicit merchant approval. |
| `SHOPIFY_DISCOUNT_TEST_MERCHANT_ID` | secret or variable | Merchant connected to the Shopify store under validation. |
| `SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY` | secret | Agent API key for the selected backend. Do not paste it into logs. |
| `SHOPIFY_DISCOUNT_TEST_PRODUCT_ID` | secret or variable | Pivota product id for the Shopify test product. Optional if the default placeholder is valid for the environment. |
| `SHOPIFY_DISCOUNT_TEST_VARIANT_ID` | secret or variable | Shopify variant id for the test product. Required. |
| `SHOPIFY_DISCOUNT_TEST_CUSTOMER_EMAIL` | secret or variable | Test buyer email. Use an address approved for the selected merchant/store. |
| `SHOPIFY_DISCOUNT_TEST_SHIPPING_COUNTRY` | secret or variable | Shipping country for delivery-rate evidence. The manual workflow defaults to `US` for US free-shipping fixtures. |
| `SHOPIFY_DISCOUNT_TEST_SHIPPING_STATE` | secret or variable | State/province code for delivery-rate evidence. |
| `SHOPIFY_DISCOUNT_TEST_SHIPPING_POSTAL_CODE` | secret or variable | Postal/ZIP code that is inside the test shipping zone. |
| `SHOPIFY_DISCOUNT_TEST_SHIPPING_CITY` | secret or variable | City for the test shipping address. |
| `SHOPIFY_DISCOUNT_TEST_SHIPPING_ADDRESS1` | secret or variable | Street line for the test shipping address. |
| `SHOPIFY_DISCOUNT_PREFLIGHT_ADMIN_KEY` | secret | Optional internal key for the read-only `discountNodes` access probe. Do not use this for public validation jobs. |

The manual workflow also accepts a dispatch-time
`shopify_discount_fixture_env_json` input for non-secret merchant, product,
variant, shipping, and discount-code fixtures. Keep
`SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY` and `SHOPIFY_DISCOUNT_PREFLIGHT_ADMIN_KEY`
as GitHub secrets; do not pass live keys through workflow inputs. The JSON
input is allowlisted and ignores secret-bearing keys.

Configure scenario fixtures as available:

| Name | Scenario |
| --- | --- |
| `SHOPIFY_DISCOUNT_TEST_AMOUNT_CODE` | Valid amount-off code |
| `SHOPIFY_DISCOUNT_TEST_AUTOMATIC_ENABLED` | Automatic amount-off discount expected when set |
| `SHOPIFY_DISCOUNT_TEST_BXGY_CODE` | Buy X Get Y code |
| `SHOPIFY_DISCOUNT_TEST_BXGY_QUANTITY` | Quantity for the BXGY fixture, defaults to `2` |
| `SHOPIFY_DISCOUNT_TEST_FREE_SHIPPING_CODE` | Free-shipping code |
| `SHOPIFY_DISCOUNT_TEST_NEW_CUSTOMER_CODE` | New-customer or customer-context code |
| `SHOPIFY_DISCOUNT_TEST_EXHAUSTED_CODE` | Usage-limit exhausted code |
| `SHOPIFY_DISCOUNT_TEST_ACTIVE_WINDOW_CODE` | Active scheduling window code |
| `SHOPIFY_DISCOUNT_TEST_INACTIVE_WINDOW_CODE` | Inactive scheduling window code |
| `SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_A` | First combinable code |
| `SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_B` | Second combinable code |
| `SHOPIFY_DISCOUNT_TEST_COMBINABLE_QUANTITY` | Quantity for the combinable discount probe, defaults to `1` |
| `SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A` | First non-combinable/conflict code |
| `SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_B` | Second non-combinable/conflict code |
| `SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_QUANTITY` | Quantity for the non-combinable conflict probe, defaults to the BXGY quantity |

## Manual GitHub Actions run

The validation job is embedded in `Agent Reliability Suite` so it can be
dispatched against a PR branch before this branch is merged to `main`.

Run dev/test quote validation:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref main \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=false \
  -f include_shopify_order_create=false
```

Run live quote/cart validation only after the merchant has approved live testing:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref main \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=true \
  -f include_shopify_order_create=false \
  -f shopify_discount_test_base_url=https://api.pivota.cc \
  -f shopify_discount_fixture_env_json='{"SHOPIFY_DISCOUNT_TEST_MERCHANT_ID":"merch_test","SHOPIFY_DISCOUNT_TEST_PRODUCT_ID":"shopify_product_id","SHOPIFY_DISCOUNT_TEST_VARIANT_ID":"shopify_variant_id","SHOPIFY_DISCOUNT_TEST_AMOUNT_CODE":"PIVOTA_TEST_AMOUNT10","SHOPIFY_DISCOUNT_TEST_BXGY_CODE":"PIVOTA_TEST_BXGY","SHOPIFY_DISCOUNT_TEST_BXGY_QUANTITY":"3","SHOPIFY_DISCOUNT_TEST_FREE_SHIPPING_CODE":"PIVOTA_TEST_FREESHIP","SHOPIFY_DISCOUNT_TEST_EXHAUSTED_CODE":"PIVOTA_TEST_EXHAUSTED","SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_A":"PIVOTA_TEST_AMOUNT10","SHOPIFY_DISCOUNT_TEST_COMBINABLE_CODE_B":"PIVOTA_TEST_COMBO_A","SHOPIFY_DISCOUNT_TEST_COMBINABLE_QUANTITY":"3","SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A":"PIVOTA_TEST_NOCOMBO_A","SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_B":"PIVOTA_TEST_BXGY","SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_QUANTITY":"3","SHOPIFY_DISCOUNT_TEST_SHIPPING_COUNTRY":"US","SHOPIFY_DISCOUNT_TEST_SHIPPING_STATE":"NY","SHOPIFY_DISCOUNT_TEST_SHIPPING_CITY":"New York","SHOPIFY_DISCOUNT_TEST_SHIPPING_POSTAL_CODE":"10118","SHOPIFY_DISCOUNT_TEST_SHIPPING_ADDRESS1":"350 Fifth Avenue"}'
```

Run quote-to-order validation only after the merchant, Shopify store, and test
PSP path are confirmed safe. Do not combine this with `allow_live_no_order=true`:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref main \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=false \
  -f include_shopify_order_create=true
```

Download the generated artifact named `shopify-discount-validation-<run_id>`.
Evidence is written under `artifacts/shopify-discount-validation/<run_id>/`.
The workflow runs `scripts/preflight_shopify_discounts.py` first and stores
setup evidence under `preflight/`; the quote matrix is stored under
`quote-matrix/`.

## Local run

Run the preflight first. It separates Shopify fixture/scope/shipping setup
blockers from quote-path regressions and never creates orders, confirms PSP
payments, or writes Shopify discounts:

```bash
python3 scripts/preflight_shopify_discounts.py \
  --allow-live-readonly \
  --allow-remote \
  --output-dir artifacts/shopify-discount-validation/preflight-live
```

If `SHOPIFY_DISCOUNT_PREFLIGHT_ADMIN_KEY` is present, the preflight also calls
the internal read-only discount-node probe:

```bash
GET /agent/internal/shopify/promotions/preflight/{merchant_id}/discount-nodes
```

That endpoint checks the stored Shopify Admin token scopes and runs `discountNodes(first: 1)`.
For merchants connected through their own Shopify custom app, `read_discounts`
must be enabled on that custom app and the stored Admin API access token must be
regenerated/updated before this probe can pass.
It does not call the promotion sync/upsert path.

Use local runs with dev/test fixtures:

```bash
python3 scripts/validate_shopify_discounts.py \
  --allow-dev-store \
  --allow-remote \
  --output-dir artifacts/shopify-discount-validation/local
```

Use local live quote/cart validation only with merchant-approved fixtures:

```bash
python3 scripts/validate_shopify_discounts.py \
  --allow-live-no-order \
  --allow-remote \
  --output-dir artifacts/shopify-discount-validation/live-no-order
```

For order creation:

```bash
SHOPIFY_DISCOUNT_TEST_ORDER_CREATE=1 \
python3 scripts/validate_shopify_discounts.py \
  --allow-dev-store \
  --allow-remote \
  --include-order-create \
  --output-dir artifacts/shopify-discount-validation/local-order-create
```

The harness blocks `--include-order-create` when `--allow-live-no-order` is set.

## Paid canary audit

After an approved paid discounted order canary, run the read-only ledger audit
against the same environment:

```bash
python3 scripts/check_discount_order_canaries.py \
  --merchant-id "$SHOPIFY_DISCOUNT_TEST_MERCHANT_ID" \
  --output-dir artifacts/shopify-discount-validation/order-canaries
```

This checks discounted paid orders for missing Shopify order links,
non-authoritative quote evidence, refund ledger totals greater than the order
total, and Shopify refund webhooks that were not ignored for external PSP
orders.

## Pass criteria

Required before converting the PR out of draft:

- Valid amount-off code returns `applicable=true`, discount evidence, and a
  positive discount amount.
- Invalid code returns `applicable=false` and no discount amount.
- Automatic discount evidence appears when the dev-store fixture is enabled.
- BXGY and free-shipping fixtures produce Shopify evidence without fabricated
  free-shipping amounts.
- Shipping fee is accepted as authoritative only when Shopify returns a selected
  delivery option. Delta-based shipping inference is default-off; do not enable
  `SHOPIFY_STOREFRONT_SHIPPING_FALLBACK_INFER=1` for pilot charge paths.
- New-customer/customer-context validation reports Shopify eligibility evidence
  or remains blocked/unverified without locally applying a manual new-customer
  promotion.
- Usage-limit and active-window negative fixtures are rejected cleanly.
- Combinable and non-combinable code behavior is captured in evidence.
- Quote-to-order validation reconciles Pivota quote total, external PSP/test
  transaction amount, Shopify order total, and Shopify discount total.

## Release decision

Keep `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=observe` while collecting validation
evidence. Pilot merchants must use `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`
and merchant allowlisting before any order/payment path is enabled. Production
order creation remains blocked until dev-store quote-to-order artifacts show the
Shopify order totals match the Pivota quote and PSP transaction.
