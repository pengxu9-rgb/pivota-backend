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

Each `ReadyVariant` now includes:

- identity: `variant_id`, `sku`, `title`, `attributes`
- commercial state: `price`, `inventory`
- `freshness`
- `provenance`
- `source_of_truth`
- `blockers`
- `warnings`
- `discovery`
- `checkout`
- `channel_coverage`

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

## Event Model

Synthetic path retains:

- `checkout_created`
- `payment_stubbed`
- `order_created`
- `order_forwarded_to_merchant_stub`
- `state_synced`

Real merchant alpha adds:

- `payment_capability_verified`
- `checkout_blocked`
- `order_forwarded_to_merchant`
- `merchant_writeback_failed`

Replay remains idempotent by `checkout_id + event_type`.

## Golden Examples

Synthetic summary goldens:

- `readiness/fixtures/golden_readiness_report_ucp.json`
- `readiness/fixtures/golden_ucp_export.json`

Real merchant alpha summary goldens:

- `readiness/fixtures/golden_real_merchant_readiness_report_ucp.json`
- `readiness/fixtures/golden_real_merchant_ucp_export.json`
- `readiness/fixtures/golden_real_merchant_blocked_checkout.json`
- `readiness/fixtures/golden_real_merchant_order_sync.json`

## Non-Goals In This Version

- full merchant-native PSP execution on readiness routes
- live webhook-driven payment confirmation
- multi-merchant rollout
- Google Merchant Center export
- ChatGPT product-feed production adapter
- normalized product reviews/confidence
