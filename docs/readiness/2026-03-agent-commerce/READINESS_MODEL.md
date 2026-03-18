# Readiness Model

## Canonical Architecture

1. `merchant source adapters`
   - `readiness/sources/synthetic.py`
   - `readiness/sources/shopify_live.py`
2. `source-of-truth policy engine`
   - `readiness/source_of_truth.py`
3. `canonical readiness snapshot`
   - `readiness/scoring.py`
4. `channel export layer`
   - `readiness/channel_exports/ucp.py`
5. `checkout and order-sync orchestration`
   - `readiness/service.py`
   - `readiness/order_sync.py`
6. `internal alpha surface`
   - `routes/readiness_internal.py`

## Current Alpha Source Of Truth

| Field Family | Canonical | Fallback | Freshness |
| --- | --- | --- | --- |
| catalog/title/description/media | normalized Shopify cache | Shopify Admin product fetch | 24h |
| price/currency | normalized Shopify variant offer | Shopify Admin product fetch | 1h |
| inventory/availability | Shopify inventory intent | cached Shopify inventory | 15m |
| fulfillment/returns policy | `readiness.alpha_policy_config.v1` | none | 30d |
| checkout capability | readiness capability resolver | none | realtime |
| order status | readiness order-sync journal | local `orders` row | realtime local / async merchant |
| reviews/confidence | Reviews Center `review_group` summary | Reviews Center `product_reviews` summary | 30d |

Synthetic mode keeps older offline freshness tolerances so the test harness remains stable.

## `MerchantReadinessSnapshot`

Top-level fields:

- `report_version`
- `merchant_id`
- `merchant_name`
- `channel`
- `generated_at`
- `merchant_alpha_mode`
- `readiness_score`
- `domain_scores`
- `capability_status`
- `blockers`
- `warnings`
- `merchant_capabilities`
- `channel_coverage`
- `source_of_truth`
- `stubbed_capabilities`
- `audit_notes`
- `products`

Optional lightweight response mode:

- `response_mode=summary`
- `summary.product_count`
- `summary.variant_count`
- `summary.ready_variant_count`
- `summary.blocked_variant_count`
- `summary.product_ids_sample`
- `summary.ready_variant_ids_sample`
- `summary.blocked_variant_ids_sample`
- `summary.blocked_checkout_reason_counts`
- `summary.blocked_discovery_reason_counts`
- `summary.products_with_reviews`
- `summary.grouped_products_with_reviews`
- `summary.sample_limit`

When `summary_only=true` is passed to the internal report route, `products` is intentionally returned as `[]` and the compact `summary` object becomes the canonical operator-facing payload.

Each `ReadyVariant` now includes:

- identity: `variant_id`, `sku`, `title`, `attributes`
- commercial state: `price`, `inventory`
- `freshness`
- `provenance`
- `source_of_truth`
- `reviews`
- `blockers`
- `warnings`
- `discovery`
- `checkout`
- `channel_coverage`

Each `ReadyProduct` now also carries:

- `reviews`

## `ChannelReadinessReport`

Top-level fields:

- `export_version`
- `merchant_id`
- `channel`
- `generated_at`
- `merchant_alpha_mode`
- `readiness_score`
- `capability_status`
- `blockers`
- `warnings`
- `source_of_truth`
- `validation_warnings`
- `stubbed_capabilities`
- `offers`

Optional lightweight response mode:

- `response_mode=summary`
- `summary.offer_count`
- `summary.review_backed_offer_count`
- `summary.availability_counts`
- `summary.currency_counts`
- `summary.offer_ids_sample`
- `summary.product_ids_sample`
- `summary.sample_limit`

When `summary_only=true` is passed to the internal export route, `offers` is intentionally returned as `[]` and the compact `summary` object becomes the canonical operator-facing payload.

Each UCP offer includes:

- `offer_id`
- `merchant_id`, `product_id`, `variant_id`
- `title`, `variant_title`, `description`, `brand`, `category`, `image_url`
- `price`
- `availability`, `inventory_quantity`
- `attributes`
- `shipping_summary`, `returns_summary`
- `checkout_capability`
- `readiness`
- `source_of_truth`
- `freshness`
- `reviews`

## Checkout Contract

Request:

```json
{
  "variant_id": "431000000001",
  "quantity": 2,
  "idempotency_key": "alpha-1",
  "buyer_email": "buyer@example.com",
  "customer_name": "Alpha Buyer",
  "shipping_address": {
    "name": "Alpha Buyer",
    "address_line1": "1 Orchard Road",
    "city": "Singapore",
    "postal_code": "238823",
    "country": "SG"
  }
}
```

Response adds:

- `merchant_alpha_mode`
- `capability_status`
- `blockers`
- `warnings`
- `source_of_truth`

Blocked checkout contract:

- HTTP status: `409`
- top-level unified error code: `VARIANT_NOT_READY_FOR_CHECKOUT`
- detail payload retains:
  - `code`
  - `variant_id`
  - `blockers`
  - `warnings`

## Payment Bridge Contract

Readiness alpha now exposes an additive internal bridge for attaching externally executed PSP state to a readiness-created order:

- `POST /internal/readiness/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-bridge`

Request:

```json
{
  "payment_reference": "pi_live_123",
  "psp_used": "stripe",
  "source": "operator_canary_bridge",
  "mark_paid": true,
  "sync_shopify_transaction": true
}
```

Response fields:

- `merchant_id`
- `merchant_alpha_mode`
- `checkout_id`
- `order_id`
- `status`
- `payment_status`
- `payment_reference`
- `psp_used`
- `transaction_sync`
- `replayed`
- `events`

Current semantics:

- the bridge is internal-only and feature-flagged
- it does not execute a PSP authorization itself
- it attaches an already successful external payment reference to the local `orders` row
- it marks the order `paid`
- it best-effort syncs a matching Shopify transaction when the readiness order is already linked to Shopify
- it makes the readiness order refund-eligible for the existing refund path

## Order Sync Contract

Request:

```json
{
  "replay": false
}
```

Response fields:

- `merchant_id`
- `merchant_alpha_mode`
- `checkout_id`
- `order_id`
- `status`
- `replayed`
- `events`
- `capability_status`
- `source_of_truth`
- `todo`

Missing checkout contract:

- `GET /internal/readiness/checkout-sessions/{checkout_id}`
- `POST /internal/readiness/merchants/{merchant_id}/order-sync/{checkout_id}`
- HTTP status: `404`
- top-level unified error code: `CHECKOUT_NOT_FOUND`

## Order Sync Audit Contract

Read-only internal audit surface:

- `GET /internal/readiness/merchants/{merchant_id}/order-sync-audit/{checkout_id}?sample_limit=10`

Response fields:

- `merchant_id`
- `checkout_id`
- `merchant_alpha_mode`
- `checkout_status`
- `order_id`
- `shopify_order_id`
- `source_of_truth`
- `order_state`
- `sync_signals.merchant_writeback`
- `sync_signals.webhook_ingest`
- `sync_signals.cancellation_sync`
- `sync_signals.refund_sync`
- `sync_signals.return_sync`
- `warnings`
- `recommendations`
- `evidence.readiness_event_types`
- `evidence.order_events`
- `evidence.webhook_events`
- `evidence.refund_records`
- `evidence.return_records`

Current signal semantics:

- `merchant_writeback`
  - `ready`: local order links to a non-null `orders.shopify_order_id`
  - `pending`: local order exists but merchant write-back is not yet confirmed
  - `blocked`: readiness journal contains `merchant_writeback_failed`
- `webhook_ingest`
  - `ready`: matching rows exist in `pcs_shopify_webhook_events`
  - `pending`: merchant write-back succeeded but webhook evidence has not landed yet
- `cancellation_sync`
  - `ready`: `orders.status=cancelled` or `orders/cancelled` / `order_cancelled_webhook` evidence is present
  - `not_observed`: no cancellation evidence yet
- `refund_sync`
  - `ready`: `refund_records`, refund webhook topics, or refunded order state is present
  - `not_observed`: the order is refund-eligible but no refund evidence has landed yet
  - `not_eligible`: the order is still unpaid or missing a payment reference, so refund validation is not yet meaningful
- `return_sync`
  - `ready`: `return_records` or Shopify `returns/*` webhook evidence is present
  - `not_observed`: no return evidence yet

## Event Model

Synthetic path retains:

- `checkout_created`
- `payment_stubbed`
- `order_created`
- `order_forwarded_to_merchant_stub`
- `state_synced`

Real merchant alpha adds:

- `payment_capability_verified`
- `payment_reference_attached`
- `merchant_payment_transaction_synced`
- `checkout_blocked`
- `order_forwarded_to_merchant`
- `merchant_writeback_failed`
- `merchant_cancellation_observed`
- `merchant_refund_observed`
- `merchant_partial_refund_observed`

Replay remains idempotent by `checkout_id + event_type`.

For real-merchant alpha checkouts, replay is also the canonical convergence hook after downstream merchant-side state changes:

- if `orders.status=cancelled`, replay upgrades the readiness checkout session to `cancelled`
- if `orders.payment_status=refunded`, replay upgrades the readiness checkout session to `refunded`
- if `orders.payment_status=partially_refunded` or `orders.total_refunded>0`, replay upgrades the readiness checkout session to `partially_refunded`

## Golden Examples

Synthetic summary goldens:

- `readiness/fixtures/golden_readiness_report_ucp.json`
- `readiness/fixtures/golden_ucp_export.json`

Real merchant alpha summary goldens:

- `readiness/fixtures/golden_real_merchant_readiness_report_ucp.json`
- `readiness/fixtures/golden_real_merchant_ucp_export.json`
- `readiness/fixtures/golden_real_merchant_blocked_checkout.json`
- `readiness/fixtures/golden_real_merchant_order_sync.json`

## Internal Error Contract

Readiness internal routes now preserve explicit machine-readable top-level error codes through the global error middleware:

- `READINESS_MERCHANT_UNSUPPORTED`
- `UNSUPPORTED_CHANNEL`
- `VARIANT_NOT_FOUND`
- `VARIANT_NOT_READY_FOR_CHECKOUT`
- `CHECKOUT_NOT_FOUND`
- `CHECKOUT_INVALID`
- `CHECKOUT_ORDER_NOT_CREATED`
- `ORDER_ALREADY_PAID`

Current endpoint matrix:

| Surface | HTTP Status | Top-Level Error Code |
| --- | --- | --- |
| `GET /internal/readiness/merchants/{merchant_id}/report` with unsupported merchant | `404` | `READINESS_MERCHANT_UNSUPPORTED` |
| `GET /internal/readiness/merchants/{merchant_id}/report` or `/exports/ucp` with unsupported channel | `400` | `UNSUPPORTED_CHANNEL` |
| `POST /internal/readiness/merchants/{merchant_id}/checkout` with unknown variant | `404` | `VARIANT_NOT_FOUND` |
| `POST /internal/readiness/merchants/{merchant_id}/checkout` with blocked variant | `409` | `VARIANT_NOT_READY_FOR_CHECKOUT` |
| `GET /internal/readiness/checkout-sessions/{checkout_id}` with unknown checkout | `404` | `CHECKOUT_NOT_FOUND` |
| `POST /internal/readiness/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-bridge` before local order creation | `409` | `CHECKOUT_ORDER_NOT_CREATED` |
| `POST /internal/readiness/merchants/{merchant_id}/checkout-sessions/{checkout_id}/payment-bridge` for already-paid order with different reference | `409` | `ORDER_ALREADY_PAID` |
| `POST /internal/readiness/merchants/{merchant_id}/order-sync/{checkout_id}` with unknown checkout | `404` | `CHECKOUT_NOT_FOUND` |
| `POST /internal/readiness/merchants/{merchant_id}/checkout` or `/order-sync/{checkout_id}` with invalid request shape | `400` | `CHECKOUT_INVALID` |

## Non-Goals In This Version

- full merchant-native PSP execution on readiness routes
- live webhook-driven payment confirmation
- multi-merchant rollout
- Google Merchant Center export
- ChatGPT product-feed production adapter
- full review freshness/ranking normalization beyond the current product-level Reviews Center projection
