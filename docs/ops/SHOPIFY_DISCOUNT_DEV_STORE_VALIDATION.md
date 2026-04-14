# Shopify Discount Dev-Store Validation

Use this gate before merging Shopify discount execution changes. It must target
only a dev/test Pivota merchant, a Shopify dev/test store, test products, and
test PSP/order paths. Do not use production stores, live discounts, live
checkout traffic, live orders, or live credentials.

## Required fixtures

Configure these as GitHub Actions secrets or variables before running the
manual workflow:

| Name | Secret or variable | Notes |
| --- | --- | --- |
| `SHOPIFY_DISCOUNT_TEST_BASE_URL` | secret or variable | Pivota dev/test backend URL. Use `allow_remote_dev_url=true` only for non-local dev/test URLs. |
| `SHOPIFY_DISCOUNT_TEST_MERCHANT_ID` | secret or variable | Dev/test merchant connected to a Shopify dev/test store. |
| `SHOPIFY_DISCOUNT_TEST_AGENT_API_KEY` | secret | Agent API key for the dev/test backend only. |
| `SHOPIFY_DISCOUNT_TEST_PRODUCT_ID` | secret or variable | Pivota product id for the Shopify test product. Optional if the default placeholder is valid for the environment. |
| `SHOPIFY_DISCOUNT_TEST_VARIANT_ID` | secret or variable | Shopify variant id for the test product. Required. |
| `SHOPIFY_DISCOUNT_TEST_CUSTOMER_EMAIL` | secret or variable | Test buyer email. Use an address safe for dev-store customer lookup. |

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

Run read-only quote validation:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref codex/shopify-discount-repair-20260414 \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f include_shopify_order_create=false
```

Run quote-to-order validation only after the merchant, Shopify store, and test
PSP path are confirmed safe:

```bash
gh workflow run agent-reliability-suite.yml \
  --repo pengxu9-rgb/pivota-backend \
  --ref codex/shopify-discount-repair-20260414 \
  -f run_shopify_discount_validation=true \
  -f allow_remote_dev_url=true \
  -f include_shopify_order_create=true
```

Download the generated artifact named `shopify-discount-validation-<run_id>`.
Evidence is written under `artifacts/shopify-discount-validation/<run_id>/`.

## Local run

Use local runs only when the shell is configured with dev/test fixtures:

```bash
python3 scripts/validate_shopify_discounts.py \
  --allow-dev-store \
  --allow-remote \
  --output-dir artifacts/shopify-discount-validation/local
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

Keep `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=observe` while collecting dev-store
evidence. Pilot merchants must use `SHOPIFY_DISCOUNT_RECONCILIATION_MODE=fail_closed`
and merchant allowlisting. Production rollout remains blocked until dev-store
validation artifacts show the checkout/order totals match the Pivota quote.
