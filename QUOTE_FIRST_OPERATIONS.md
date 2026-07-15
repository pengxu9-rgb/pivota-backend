# Quote-First Operations (v0.2-a hardening)

This doc is an operator/developer runbook for quote-first: idempotency, enforcement, and drift diagnostics.

Related spec: `external_repos/pivota-backend/PCS_V0_2_QUOTE_FIRST.md`.

---

## 1) Feature Flags / Rollout

### Quote TTL

- `QUOTE_TTL_SECONDS` (default: `600`)
  - Controls how long a quote remains `active` before it becomes `expired`.

### Enforcement modes

1) **Global require** (strongest; easiest rollback):
- `FF_ENABLE_QUOTE_FIRST_ORDER_CREATE=true`
  - `quote_id` is required on `/agent/v1/orders/create` and `/orders/create`.

2) **Tiered require** (recommended rollout):
- `FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT=true`
- `FF_QUOTE_FIRST_MIN_TIER=L1C` (default: `L1C`)
- `FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS=m_123,m_456` (optional allowlist override)

Behavior:
- If merchant is allowlisted → require `quote_id`.
- Else require when `merchant_tier >= min_tier`.

### Rollback

- To disable enforcement: set `FF_ENABLE_QUOTE_FIRST_ORDER_CREATE=false` and `FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT=false`.
- Drift diagnostics + events are best-effort and safe to leave enabled.

### Shopify Pricing Engine Notes (important)

Quote preview (`POST /agent/v1/quotes/preview`) needs a Shopify pricing engine that can compute totals.

- Legacy engine: **Admin REST Checkout API** (`/admin/api/*/checkouts.json`)
  - Shopify may return `403` with: `This action requires merchant approval for write_checkouts scope.`
  - Many modern apps do not have `write_checkouts` available → this engine will fail in production.
- Current preferred engine: **Shopify Storefront Cart API** (`/api/*/graphql.json` + `cartCreate`)
  - Requires a Storefront access token (`X-Shopify-Storefront-Access-Token`).
  - Deployment shortcut: set `SHOPIFY_STOREFRONT_ACCESS_TOKEN` in the backend env.
  - Longer-term: store a per-merchant storefront token in `merchant_stores.api_key` JSON under `storefront_access_token`.
  - Note: Storefront can return an empty `deliveryGroups` array when the shop has no shipping rates configured for the provided address; in that case backend preflight will warn with `delivery_options_unavailable` and you should treat shipping fee as “confirmed at checkout”.

---

## 2) Telemetry Signals (No PII)

Events are appended to `mvp_events` (or `mvp_events.jsonl` if file sink is configured).

New event types:
- `quote_required_blocked`: enforcement rejected missing `quote_id`
- `quote_drift_detected`: backend rejected order due to `QUOTE_MISMATCH` (includes drift details)
- `quote_consumed`: quote successfully used by an order create

---

## 3) SQL: Drift Distribution / Debug

### 3.1 Drift fields distribution (last 7d)

```sql
SELECT
  drift_field,
  COUNT(*) AS n
FROM mvp_events e
CROSS JOIN LATERAL jsonb_array_elements_text(
  (e.payload_json->'drift'->'drift_fields')::jsonb
) AS drift_field
WHERE e.event_type = 'quote_drift_detected'
  AND e.occurred_at > NOW() - INTERVAL '7 days'
GROUP BY 1
ORDER BY n DESC;
```

### 3.2 Drift fields by merchant (last 7d)

```sql
SELECT
  e.merchant_id,
  drift_field,
  COUNT(*) AS n
FROM mvp_events e
CROSS JOIN LATERAL jsonb_array_elements_text(
  (e.payload_json->'drift'->'drift_fields')::jsonb
) AS drift_field
WHERE e.event_type = 'quote_drift_detected'
  AND e.occurred_at > NOW() - INTERVAL '7 days'
GROUP BY 1, 2
ORDER BY n DESC;
```

### 3.3 Debug a specific quote_id

```sql
SELECT
  occurred_at,
  merchant_id,
  payload_json
FROM mvp_events
WHERE event_type = 'quote_drift_detected'
  AND payload_json->>'quote_id' = 'q_xxx'
ORDER BY occurred_at DESC
LIMIT 20;
```

The drift payload includes:
- `quote_id`, `quote_expires_at`, `quote_hash_sha256`
- `quote_request_fingerprint`, `order_request_fingerprint`
- `drift_fields`
- `quote_request_normalized`, `order_request_normalized` (safe fields only)

---

## 4) Quote / Order / Idempotency Debug

### 4.1 Quote row

```sql
SELECT
  quote_id,
  merchant_id,
  status,
  expires_at,
  consumed_at,
  consumed_order_id,
  request_fingerprint,
  quote_hash_sha256,
  debug_id
FROM quotes
WHERE quote_id = 'q_xxx';
```

### 4.2 Order row (includes `pricing_quote` snapshot)

```sql
SELECT
  order_id,
  merchant_id,
  status,
  payment_status,
  total,
  currency,
  created_at,
  (metadata->'pricing_quote') AS pricing_quote
FROM orders
WHERE order_id = 'ord_xxx';
```

### 4.3 Order create replay (idempotency cache)

```sql
SELECT
  scope,
  idem_key,
  created_at,
  value_json->>'order_id' AS order_id
FROM mvp_idempotency_keys
WHERE scope = 'order_create'
  AND idem_key = 'idem_key_xxx';
```

---

## 5) Common Failure Patterns (Playbook)

### `QUOTE_REQUIRED`
- Meaning: enforcement is enabled for this merchant but `quote_id` is missing.
- Action:
  - Confirm flags for the environment.
  - Confirm merchant is allowlisted / tiered into the requirement.
  - Ensure caller passes `quote_id` from `/agent/v1/quotes/preview` into `/agent/v1/orders/create`.

### `QUOTE_EXPIRED`
- Meaning: quote TTL passed.
- Action: generate a new quote and retry order creation.

### `QUOTE_CONSUMED`
- Meaning: the quote was already used; `details.order_id` may be present.
- Action:
  - Treat as idempotency: fetch the referenced `order_id` and continue the flow.
  - If `order_id` is missing, inspect `mvp_idempotency_keys` (scope `order_create`) for the idempotency key used by the caller.

### `QUOTE_MISMATCH`
- Meaning: order payload fingerprint does not match quote snapshot.
- Action:
  - Inspect the `details` payload in the 409 response (or the `quote_drift_detected` event).
  - Fix the caller to ensure order create uses the same:
    - items + quantities
    - discount codes
    - shipping geo (country/postal/city/state)
    - selected delivery option identifier

### Payment replay / double execution concerns
- Use `/agent/v1/payments` with `idempotency_key` for safe retries.
- If duplicates are suspected, query `payments` by `(order_id, idempotency_key)` and validate that only one row exists.


## 6) Post‑Sale Risk: Disputes (Chargebacks) + Returns (v0.2)

This repo currently supports **signals + ops visibility** (not a full CS/returns workflow yet).

### 6.1 Stripe chargebacks (disputes)

- **Webhook ingestion**: `POST /webhooks/stripe`
  - Handles `charge.dispute.*` (best‑effort) and upserts `dispute_records`.
  - Does **not** auto‑mutate order state (treat as risk/ops signal).
- **Stripe webhook topics to enable** (recommended):
  - `charge.dispute.created`
  - `charge.dispute.updated`
  - `charge.dispute.closed`
  - (optional) `charge.dispute.funds_withdrawn`, `charge.dispute.funds_reinstated`

### 6.2 Shopify disputes + returns

- **Disputes**:
  - Webhooks: `disputes/create`, `disputes/update` are already handled in `routes/webhook_routes.py`.
  - Best‑effort:
    - append order event + telemetry
    - create PCS `dispute_pack` evidence pack
    - upsert `dispute_records` for ops list visibility
- **Returns**:
  - Webhooks: `returns/create`, `returns/update` are attempted during onboarding/resubscribe.
  - Reality: Shopify topic availability varies by shop/app; you may see 404 “topic not found”.
  - Fallback: use the **admin sync** endpoint to pull latest returns via Admin GraphQL and upsert `return_records`.

### 6.3 Ops endpoints (admin-key protected)

Backend (pivota-backend):
- `GET /agent/internal/disputes`
- `GET /agent/internal/returns`
- `POST /agent/internal/returns/sync?merchantId=...&limit=20&apiVersion=2025-10`

Gateway (PIVOTA-Agent):
- `GET /api/merchant/disputes`
- `GET /api/merchant/returns`
- `POST /api/merchant/returns/sync` (JSON: `{ "merchantId": "...", "limit": 20 }`)

Creator UI (pivota-creator-ui):
- `GET /ops/disputes` (reads `/api/ops/disputes`)
- `GET /ops/returns` (reads `/api/ops/returns` + `/api/ops/returns/sync`)

### 6.4 Database tables

- `dispute_records` (Stripe + Shopify disputes)
- `return_records` (Shopify returns)

Migration: `db/migrations/035_disputes_and_returns.sql`
