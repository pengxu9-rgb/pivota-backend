# Developer Runbook

## Flags

```bash
export FEATURE_READINESS_AUDIT=true
export FEATURE_READINESS_UCP_THIN_SLICE=true
export FEATURE_READINESS_REAL_MERCHANT_ALPHA=true
export FEATURE_READINESS_SOURCE_OF_TRUTH_V1=true
export FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA=true
export FEATURE_READINESS_PAYMENT_BRIDGE_ALPHA=true
export FEATURE_READINESS_PAYMENT_INTENT_ALPHA=true
export READINESS_ALPHA_MERCHANT_ID=merch_efbc46b4619cfbdf
export READINESS_INTERNAL_API_KEY=change-me
export DATABASE_URL='postgresql://...'
```

## Test

```bash
python3 -m pytest readiness/tests -q
```

## Start Local API

```bash
uvicorn main:app --reload --port 8000
```

## Production Smoke Script

Read-only by default:

```bash
bash scripts/smoke_readiness_alpha.sh \
  --base-url https://<prod-host> \
  --internal-key "$READINESS_INTERNAL_API_KEY"
```

The smoke script now uses the lightweight readiness summary views for its read-only report/export steps so production probes do not have to stream full product or offer payloads.

Single supervised live canary:

```bash
bash scripts/smoke_readiness_alpha.sh \
  --base-url https://<prod-host> \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --canary-write
```

If you already have a successful PSP payment reference from an external execution path, the smoke script can bridge it into the readiness order after the canary write:

```bash
bash scripts/smoke_readiness_alpha.sh \
  --base-url https://<prod-host> \
  --internal-key "$READINESS_INTERNAL_API_KEY" \
  --canary-write \
  --payment-reference pi_live_123 \
  --payment-psp stripe
```

The script writes all responses to `/tmp/pivota-readiness-smoke-<run_id>` unless `--out-dir` is provided.

Read-only smoke artifacts are summary-only by design. Inspect the saved artifacts with compact `jq` summaries instead of relying on the script's full stdout:

```bash
jq '{
  merchant_id,
  response_mode,
  readiness_score,
  reviews_capability: .capability_status.reviews_confidence,
  source_of_truth,
  summary
}' /tmp/pivota-readiness-smoke-<run_id>/report.json

jq '{
  merchant_id,
  response_mode,
  readiness_score,
  source_of_truth,
  validation_warnings,
  summary
}' /tmp/pivota-readiness-smoke-<run_id>/export_ucp.json
```

## Report

```bash
curl -s \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/report?channel=ucp"
```

Compact summary view:

```bash
curl -s \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/report?channel=ucp&summary_only=true&sample_limit=25"
```

## Export

```bash
curl -s \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/exports/ucp"
```

Compact summary view:

```bash
curl -s \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/exports/ucp?summary_only=true&sample_limit=25"
```

## Checkout

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/checkout" \
  -d '{
    "variant_id": "431000000001",
    "quantity": 1,
    "idempotency_key": "alpha-checkout-1",
    "buyer_email": "buyer@example.com",
    "customer_name": "Alpha Buyer",
    "shipping_address": {
      "name": "Alpha Buyer",
      "address_line1": "1 Orchard Road",
      "city": "Singapore",
      "postal_code": "238823",
      "country": "SG"
    }
  }'
```

## Order Sync

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/order-sync/<checkout_id>" \
  -d '{"replay": false}'
```

## Order Sync Audit

After a live canary write, use the sync audit to check whether the readiness journal, `orders`, Shopify webhook ingest, `refund_records`, and `return_records` are converging:

```bash
curl -s \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/order-sync-audit/<checkout_id>?sample_limit=10"
```

Compact audit view:

```bash
jq '{
  checkout_id,
  order_id,
  shopify_order_id,
  checkout_status,
  order_state,
  sync_signals,
  warnings,
  recommendations
}' /tmp/pivota-readiness-smoke-<run_id>/order_sync_audit.json
```

The production smoke script now fetches this audit automatically after a canary write and fails if `sync_signals.merchant_writeback.status != "ready"`.

If a downstream merchant-side cancellation or refund occurs after the initial readiness write-through, replay the readiness order-sync to absorb the new terminal state into the readiness journal:

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/order-sync/<checkout_id>" \
  -d '{"replay": true}'
```

Expected replay outcomes for real-merchant alpha:

- `cancelled` after a merchant-side cancellation has landed in `orders`
- `refunded` after a full refund has landed in `orders.payment_status`
- `partially_refunded` after partial refund evidence lands in `orders.total_refunded`

## Payment Bridge

If payment execution happened outside the readiness router but you need the readiness order to become refund-eligible, attach the successful PSP reference to the readiness checkout:

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/checkout-sessions/<checkout_id>/payment-bridge" \
  -d '{
    "payment_reference": "pi_live_123",
    "psp_used": "stripe",
    "source": "operator_canary_bridge",
    "mark_paid": true,
    "sync_shopify_transaction": true
  }'
```

This bridge is intentionally narrow:

- it does not authorize or capture a payment
- it writes an already-successful payment reference back into the readiness-owned local order
- it marks the order `paid`
- it best-effort syncs a matching Shopify transaction

After the bridge, re-run the audit and confirm `refund_sync.refund_eligible=true` before attempting refund validation.

## Payment Intent

If you want readiness to mint a PSP payment intent for the readiness-owned local order instead of manually attaching an external reference, call:

```bash
curl -s -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/checkout-sessions/<checkout_id>/payment-intent" \
  -d '{
    "preferred_psps": ["stripe"]
  }'
```

This route is intentionally narrow:

- it requires `/order-sync` to have already created a local order
- it reuses the existing multi-PSP payment-intent creation stack
- it is order-idempotent; repeated calls reuse the same `payment_intent_id`
- it does not by itself guarantee a paid order unless the PSP returns immediate success or a later confirmation/webhook lands

Use this path when you want readiness to own payment-intent creation, and use `payment-bridge` when payment execution already happened elsewhere.

## Error Contract Probes

Blocked checkout should fail closed with a readiness-specific top-level code:

```bash
curl -s -o /tmp/readiness-blocked.json -w "%{http_code}\n" \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/checkout" \
  -d '{"variant_id":"52327451230536","quantity":1,"idempotency_key":"blocked-probe"}'

jq '{code: .error.code, detail: .error.details}' /tmp/readiness-blocked.json
```

Unsupported merchant and missing checkout probes should also preserve top-level readiness codes:

```bash
curl -s -o /tmp/readiness-unsupported.json -w "%{http_code}\n" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_unknown/report?channel=ucp"

jq '.error.code' /tmp/readiness-unsupported.json

curl -s -o /tmp/readiness-missing-checkout.json -w "%{http_code}\n" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/checkout-sessions/rdchk_missing"

jq '.error.code' /tmp/readiness-missing-checkout.json
```

Payment bridge failures should also preserve readiness-specific top-level codes:

```bash
curl -s -o /tmp/readiness-payment-bridge.json -w "%{http_code}\n" \
  -X POST \
  -H "Content-Type: application/json" \
  -H "X-Pivota-Internal-Key: $READINESS_INTERNAL_API_KEY" \
  "http://127.0.0.1:8000/internal/readiness/merchants/merch_efbc46b4619cfbdf/checkout-sessions/<checkout_id>/payment-bridge" \
  -d '{"payment_reference":"pi_conflict"}'

jq '.error.code' /tmp/readiness-payment-bridge.json
```

## Local Caveat

If `DATABASE_URL` is absent, local live merchant validation will not work from this checkout. In that case, run the tests only or validate against a deployment that already has merchant/store/PSP data configured.
