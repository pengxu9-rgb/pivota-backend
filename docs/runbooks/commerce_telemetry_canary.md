# Canonical commerce telemetry production canary

Use this runbook after deploying a telemetry adapter or changing the canonical
event pipeline. The harness validates deployment health, the scoped merchant
funnel read, diagnostics, signed event ingestion, retry idempotency, identifier
stitching, and funnel-stage materialization.

## Safety boundary

The harness is read-only unless `--write-canary` is supplied. Write mode appends
eight synthetic events to the selected connected store. Use a dedicated canary
merchant/store whenever possible. Event, interaction, session, click, cart,
checkout, payment, order, and refund IDs are namespaced with
`telemetry_canary_*`, and the source/surface are `ops_commerce_telemetry_canary`
and `ops_canary`.

The canary never calls a platform API, creates a real order, charges a PSP,
issues a real refund, changes webhook subscriptions, or writes buyer PII. The
amounts are synthetic analytics facts in the canary store scope only.

Deployment liveness is read from the backend's public `/health` route; `/version`
supplies the commit used by `--expected-git-sha`.

## Prerequisites

- The backend containing the adapter is deployed.
- `db/migrations/206_commerce_event_funnel_read_index.sql` is applied.
- The merchant has an active connected store matching `--store-id`.
- A merchant JWT is available for scoped analytics reads.
- Write mode additionally requires the merchant HMAC API key.

## Read-only audit

```bash
python scripts/smoke_commerce_telemetry_canary.py \
  --base-url https://api.pivota.cc \
  --merchant-id merch_canary \
  --platform cafe24 \
  --store-id store_cafe24_canary \
  --expected-git-sha <deployed-sha> \
  --merchant-jwt "$MERCHANT_JWT" \
  --output-json reports/commerce-telemetry-cafe24-audit.json \
  --output-md reports/commerce-telemetry-cafe24-audit.md
```

## End-to-end write signoff

```bash
python scripts/smoke_commerce_telemetry_canary.py \
  --base-url https://api.pivota.cc \
  --merchant-id merch_canary \
  --platform cafe24 \
  --store-id store_cafe24_canary \
  --expected-git-sha <deployed-sha> \
  --merchant-jwt "$MERCHANT_JWT" \
  --merchant-api-key "$PIVOTA_MERCHANT_API_KEY" \
  --write-canary \
  --output-json reports/commerce-telemetry-cafe24-signoff.json \
  --output-md reports/commerce-telemetry-cafe24-signoff.md
```

Write mode sends the exact same signed bytes twice. The first request may report
accepted events or duplicates when a stable `--run-id` is reused. The second
must report all eight events as duplicates. The harness then reads the exact
interaction trace and verifies these canonical stages:

```text
agent requested -> product viewed -> cart active -> checkout started
                -> payment attempted -> order created -> paid -> refunded
```

Re-run a previous proof without adding ledger events by passing its `--run-id`.

## Pilot matrix

Run one write signoff for each production pilot store:

1. Cafe24
2. Shopify after the Web Pixel bridge lands
3. WooCommerce
4. Magento / Adobe Commerce
5. SHOPLINE or Shoplazza
6. Salesforce B2C Commerce
7. Custom/headless

For native adapters, this synthetic signoff proves the shared event bus and
stitching layer. It does not replace a signed native-webhook canary. Complete
each pilot with one real platform lifecycle delivery and compare its order,
payment, and refund facts with the platform/PSP source of truth.

## Pass criteria

- Every reported step is `PASS` and the process exits `0`.
- `deployment_version` matches `--expected-git-sha` when supplied.
- `connected_store_scope` confirms the requested platform/store pair is active
  and connected before write mode can run.
- `canonical_funnel_available` confirms the canonical event store is readable.
- `idempotent_replay` reports eight duplicates.
- `stitched_interaction_trace` contains all eight expected event types.
- `funnel_stage_materialization` contains all expected stages.

Never copy JWTs, API keys, or signatures into a report. The harness redacts
credential-shaped fields and never writes its input secrets to evidence files.
