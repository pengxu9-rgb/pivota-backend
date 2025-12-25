# PCS v0.2-a — Quote-First Enforcement (Pricing Lock)

This document describes the v0.2-a hardening of quote-first order creation in `pivota-backend`.

## What this adds (backward-compatible)

- A formal DB migration for the `quotes` table (`db/migrations/033_quote_first.sql`).
- Quote hashing (`quote_hash_sha256`) and durable linkage from quote → created order (`consumed_order_id`).
- Tiered quote enforcement (optional): require `quote_id` only for merchants at or above a minimum PCS tier (default `L1C`).
- Default idempotency for quote-based order creation: if `quote_id` is present and `idempotency_key` is missing, the backend sets `idempotency_key = "{merchant_id}:{quote_id}"`.
- Order snapshot EvidencePack includes a quote summary + `quote_hash_sha256` (no PII).

## Existing wiring points

- Quote preview: `POST /agent/v1/quotes/preview` (`routes/quote_routes.py`)
- Agent order create: `POST /agent/v1/orders/create` (`routes/agent_api.py`)
- Core order create: `POST /orders/create` (`routes/order_routes.py`)

## Feature flags / configuration

### Quote TTL

- `QUOTE_TTL_SECONDS` (default: `600`)

### Enforcement modes

1) Global require (existing semantics):
- `FF_ENABLE_QUOTE_FIRST_ORDER_CREATE=true`
- Behavior: `quote_id` is required for all `/agent/v1/orders/create` requests.

2) Tiered require (new, safe default off):
- `FF_ENABLE_QUOTE_FIRST_TIERED_ENFORCEMENT=true`
- `FF_QUOTE_FIRST_MIN_TIER=L1C` (default: `L1C`)
- `FF_QUOTE_FIRST_REQUIRED_MERCHANT_IDS=m_123,m_456` (optional allowlist)

Behavior:
- If merchant is on the allowlist → require `quote_id`.
- Else require `quote_id` only when `merchant_tier >= min_tier`.

## Minimal PCS tier heuristic (for rollout guards)

Implemented in `services/pcs_tier_service.py`.

- `L0`: no stored capability snapshot OR missing required scopes
- `L1`: capability snapshot exists AND missing required scopes is empty
- `L1C`: `L1` AND at least one signature-verified Shopify webhook observed in the last 7 days
- `L2`: `L1C` AND `has_shopify_payments=true`

This is intentionally minimal and conservative; it is not the full v0.2 tier/metrics engine.

## Idempotency behavior

- Quote-based checkout defaults to `idempotency_key = "{merchant_id}:{quote_id}"` when missing.
- Successful `/agent/v1/orders/create` responses are cached in `mvp_idempotency_keys` (best-effort).
- Quote rows are marked consumed with `consumed_order_id` after a successful order creation.

## Error codes (recoverable)

All errors are returned as JSON in `detail`, e.g.:

- `QUOTE_REQUIRED`: quote_id missing when enforcement requires it
- `QUOTE_NOT_FOUND`: quote_id does not exist
- `QUOTE_EXPIRED`: quote is expired (TTL)
- `QUOTE_MISMATCH`: order payload does not match quote fingerprint / merchant mismatch
- `QUOTE_CONSUMED`: quote was already used (includes `details.order_id` when available)

Example:

```json
{
  "error": "QUOTE_REQUIRED",
  "message": "quote_id is required",
  "context": {
    "mode": "tiered",
    "merchant_id": "m_123",
    "tier": "L1C",
    "min_tier": "L1C"
  }
}
```

## EvidencePack changes

Order snapshot EvidencePack (`pcs_evidence_packs.pack_type='order_snapshot'`) now includes:
- `pricing_quote`: `{ quote_id, engine, engine_ref, request_fingerprint, quote_hash_sha256, pricing, line_items, promotion_lines }`

This is best-effort and stays PII-safe.

