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
| `SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_A` | First non-combinable/conflict code |
| `SHOPIFY_DISCOUNT_TEST_NONCOMBINABLE_CODE_B` | Second non-combinable/conflict code |

## Manual GitHub Actions run

The validation job is embedded in `Agent Reliability Suite` so it can be
dispatched against a PR branch before this branch is merged to `main`.

Run dev/test quote validation:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref codex/shopify-discount-repair-20260414 \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=false \
  -f include_shopify_order_create=false
```

Run live quote/cart validation only after the merchant has approved live testing:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref codex/shopify-discount-repair-20260414 \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=true \
  -f include_shopify_order_create=false
```

Run quote-to-order validation only after the merchant, Shopify store, and test
PSP path are confirmed safe. Do not combine this with `allow_live_no_order=true`:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref codex/shopify-discount-repair-20260414 \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f allow_live_no_order=false \
  -f include_shopify_order_create=true
```

Download the generated artifact named `shopify-discount-validation-<run_id>`.
Evidence is written under `artifacts/shopify-discount-validation/<run_id>/`.

## Local run

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

## Pass criteria

Required before converting the PR out of draft:

- Valid amount-off code returns `applicable=true`, discount evidence, and a
  positive discount amount.
- Invalid code returns `applicable=false` and no discount amount.
- Automatic discount evidence appears when the dev-store fixture is enabled.
- BXGY and free-shipping fixtures produce Shopify evidence without fabricated
  free-shipping amounts.
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
