# Developer Runbook

## Flags

```bash
export FEATURE_READINESS_AUDIT=true
export FEATURE_READINESS_UCP_THIN_SLICE=true
export FEATURE_READINESS_REAL_MERCHANT_ALPHA=true
export FEATURE_READINESS_SOURCE_OF_TRUTH_V1=true
export FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA=true
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

The script writes all responses to `/tmp/pivota-readiness-smoke-<run_id>` unless `--out-dir` is provided.

For large merchants, prefer inspecting the saved artifacts with compact `jq` summaries instead of relying on the script's full stdout:

```bash
jq '{
  merchant_id,
  readiness_score,
  ready_variant_count: ([.products[].variants[] | select(.channel_coverage.ucp=="ready")] | length),
  blocked_variant_count: ([.products[].variants[] | select(.channel_coverage.ucp!="ready")] | length),
  reviews_capability: .capability_status.reviews_confidence,
  source_of_truth
}' /tmp/pivota-readiness-smoke-<run_id>/report.json

jq '{
  merchant_id,
  readiness_score,
  offer_count: (.offers | length),
  review_offer_count: ([.offers[] | select(.reviews.has_reviews==true)] | length),
  source_of_truth,
  validation_warnings
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

## Local Caveat

If `DATABASE_URL` is absent, local live merchant validation will not work from this checkout. In that case, run the tests only or validate against a deployment that already has merchant/store/PSP data configured.
