# Staging PSP Probe Runbook

Target only: `Pivota Infra / staging / web`.

Do not use production, `api.pivota.cc`, `pivota-ap2-staging`, or `web-staging`.

## Probe Fixtures

- Merchant: `merch_efbc46b4619cfbdf`
- Agent: `agent_staging_probe`
- Product: `10064562258217`
- Variant: `50000000000001`
- Quote: `q_staging_probe_10064562258217`
- PSP: `psp_stripe_testprobe001`
- Amount: `19.99 USD`
- Expected Stripe minor units: `1999`
- Stripe webhook endpoint: `https://web-staging-3f9e.up.railway.app/webhooks/stripe/psp_stripe_testprobe001`

## Required Operator Secrets

Provide real Stripe TEST values. Do not paste live keys.

```bash
export STRIPE_TEST_SECRET_KEY='sk_test_...'
export STRIPE_TEST_PUBLISHABLE_KEY='pk_test_...'
export STRIPE_TEST_WEBHOOK_SECRET='whsec_...'
```

The agent API key can be operator-provided:

```bash
export STAGING_AGENT_API_KEY='ak_live_<64 lowercase hex chars>'
```

If `STAGING_AGENT_API_KEY` is omitted, `setup_staging.sh` generates one and stores it in Railway `SHOP_GATEWAY_AGENT_API_KEY` for staging web without printing it.

## Buildout

From `/Users/pengchydan/dev/pivota-backend`:

```bash
railway status
railway add -d postgres

railway variables -e staging -s web --skip-deploys \
  --set 'DATABASE_URL=${{Postgres.DATABASE_URL}}' \
  --set 'ALLOW_TEST_PSP_PROBE=1' \
  --set 'TEST_PSP_PROBE_MERCHANTS=merch_efbc46b4619cfbdf' \
  --set 'APP_ENV=staging' \
  --set 'ENVIRONMENT=staging' \
  --set 'FRESH_QUOTE_VALIDATE_SKIP_SECONDS=86400'

railway variables -e staging -s web --skip-deploys \
  --set "SHOP_GATEWAY_AGENT_API_KEY=${STAGING_AGENT_API_KEY}"

railway up -e staging -s web --detach
```

Verify:

```bash
curl -fsS https://web-staging-3f9e.up.railway.app/version
curl -fsS https://web-staging-3f9e.up.railway.app/health
```

Expected: `/version` reports branch `main` and full SHA beginning `370a2756`; `/health` is 200 and `missing_columns` is empty.

## Seed

Run after the service is healthy and the Stripe test webhook exists:

```bash
railway run -e staging -s web -- python scripts/staging_probe/seed.py
```

Read-only verification:

```bash
railway run -e staging -s web -- python scripts/staging_probe/seed.py --verify-only
```

The seed refuses placeholder Stripe values and prints only non-secret fields.

## Gateway Environment

For an external gateway, point it at staging backend:

```bash
export AGENT_API_BASE='https://web-staging-3f9e.up.railway.app'
export SHOP_GATEWAY_AGENT_API_KEY='<same raw staging agent API key seeded above>'
```

The embedded staging gateway at `https://web-staging-3f9e.up.railway.app/agent/shop/v1/invoke` uses staging web's `SHOP_GATEWAY_AGENT_API_KEY`.

## Controlled Probe Call

Do not run this during environment setup. This is for the operator's single controlled test-mode charge.

Step 1: create the order and Stripe test PaymentIntent surface through the gateway:

```bash
curl -fsS 'https://web-staging-3f9e.up.railway.app/agent/shop/v1/invoke' \
  -H 'Content-Type: application/json' \
  -d '{
    "operation": "create_order",
    "metadata": {
      "source": "staging_psp_probe",
      "protocol_name": "rest",
      "commerce_surface": "agent_api"
    },
    "payload": {
      "order": {
        "merchant_id": "merch_efbc46b4619cfbdf",
        "customer_email": "staging-probe-buyer@pivota.invalid",
        "currency": "USD",
        "preferred_psp": "stripe",
        "quote_id": "q_staging_probe_10064562258217",
        "idempotency_key": "staging-probe-order-001",
        "metadata": {
          "allow_test_psp_surfaces": true,
          "source": "staging_psp_probe",
          "payment_return_url": "https://web-staging-3f9e.up.railway.app/probe/return"
        },
        "items": [
          {
            "merchant_id": "merch_efbc46b4619cfbdf",
            "product_id": "10064562258217",
            "product_title": "Staging Probe Product",
            "variant_id": "50000000000001",
            "sku": "PIVOTA-STAGING-PROBE-USD",
            "quantity": 1,
            "unit_price": 19.99,
            "subtotal": 19.99
          }
        ],
        "shipping_address": {
          "name": "Staging Probe Buyer",
          "address_line1": "123 Test Ave",
          "address_line2": "",
          "city": "San Francisco",
          "state": "CA",
          "country": "US",
          "postal_code": "94105",
          "phone": "+14155550100"
        }
      }
    }
  }'
```

Step 2: use the returned `order_id` for `submit_payment`:

```bash
curl -fsS 'https://web-staging-3f9e.up.railway.app/agent/shop/v1/invoke' \
  -H 'Content-Type: application/json' \
  -d '{
    "operation": "submit_payment",
    "metadata": {
      "source": "staging_psp_probe"
    },
    "payload": {
      "payment": {
        "order_id": "<ORDER_ID_FROM_STEP_1>",
        "expected_amount": 19.99,
        "currency": "USD",
        "payment_method": "card"
      }
    }
  }'
```

Then complete the Stripe test payment client-side using the returned Stripe `client_secret` and `pk_test_...`, or use the controlled operator UI that normally confirms the PaymentIntent. Verify in Stripe test mode that the PaymentIntent amount is `1999` and that the signed webhook reaches `/webhooks/stripe/psp_stripe_testprobe001`.

## Known Validation Notes

- `create_order` is where PR #738's scoped test PSP bypass is honored, because `allow_test_psp_surfaces=true` plus staging env allowlist makes `enforce_live_readiness=false`.
- `/agent/v1/payments` still enforces live PSP readiness when it must create a new PSP surface. The runbook therefore creates the PaymentIntent surface during `create_order`; `submit_payment` should reuse that surface.
- The gateway wrapper does not forward a payment idempotency key to `/agent/v1/payments`. Direct backend `/agent/v1/payments` can test payment idempotency more explicitly if the operator chooses a non-gateway verification.
