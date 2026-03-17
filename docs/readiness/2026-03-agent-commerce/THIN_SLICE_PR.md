# Thin Slice PR Summary

## What Was Implemented

- Feature-flagged internal readiness router in `routes/readiness_internal.py`
- Additive readiness package:
  - `readiness/models.py`
  - `readiness/sources/synthetic.py`
  - `readiness/scoring.py`
  - `readiness/channel_exports/ucp.py`
  - `readiness/order_sync.py`
  - `readiness/service.py`
- Synthetic merchant fixture in `readiness/fixtures/synthetic_demo_merchant.json`
- Golden fixtures for the deterministic report/export contracts:
  - `readiness/fixtures/golden_readiness_report_ucp.json`
  - `readiness/fixtures/golden_ucp_export.json`
- Route/unit tests under `readiness/tests`
- Conditional router wiring in `main.py`
- New readiness feature flags in `config/settings.py`

## Thin Slice Scope

The slice proves a vertical path for one synthetic merchant:

1. canonical readiness report
2. UCP-style readiness export
3. stubbed checkout session creation
4. stubbed order-sync journal with idempotent replay

It is intentionally conservative and does **not** claim:

- real UCP production readiness
- real PSP execution
- real merchant order write-back
- Google Merchant Center support
- normalized product reviews support

## New Endpoints

- `GET /internal/readiness/merchants/{merchant_id}/report?channel=ucp`
- `GET /internal/readiness/merchants/{merchant_id}/exports/ucp`
- `POST /internal/readiness/merchants/{merchant_id}/checkout`
- `GET /internal/readiness/checkout-sessions/{checkout_id}`
- `POST /internal/readiness/merchants/{merchant_id}/order-sync/{checkout_id}`

## Feature Flags

- `FEATURE_READINESS_AUDIT=true`
- `FEATURE_READINESS_UCP_THIN_SLICE=true`
- `READINESS_INTERNAL_API_KEY=<secret>` for internal auth
- `READINESS_ALLOW_UNAUTHED_DEV=true` for local-only testing

## How To Test

### Readiness Report

```bash
curl -s http://localhost:8000/internal/readiness/merchants/synthetic-demo-merchant/report?channel=ucp
```

### UCP Export

```bash
curl -s http://localhost:8000/internal/readiness/merchants/synthetic-demo-merchant/exports/ucp
```

### Checkout Stub

```bash
curl -s -X POST http://localhost:8000/internal/readiness/merchants/synthetic-demo-merchant/checkout \
  -H 'Content-Type: application/json' \
  -d '{"variant_id":"var_cleanser_150ml","quantity":2,"idempotency_key":"demo-1"}'
```

### Order Sync

```bash
curl -s -X POST http://localhost:8000/internal/readiness/merchants/synthetic-demo-merchant/order-sync/<checkout_id> \
  -H 'Content-Type: application/json' \
  -d '{"replay":false}'
```

## What Remains Stubbed

- payment execution
- merchant-native order write-back
- webhook verification
- non-synthetic merchant adapters

## TODOs

- replace the synthetic source with one real merchant adapter
- map real fulfillment/returns policy data into the readiness model
- bind the checkout path to one canonical PSP orchestration stack
- reconcile the mounted thin slice with the existing unmounted UCP business proxy

