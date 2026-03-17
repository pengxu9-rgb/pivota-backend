# Order Sync Decision

## Canonical Outward State

- `checkout_created`
- `blocked`
- `created`
- `forwarded`
- `state_synced`
- `failed`

## Event Strategy

The readiness journal remains the external alpha status layer. The real merchant alpha adds these meaningful events on top of the base session:

- `payment_capability_verified`
- `order_created`
- `order_forwarded_to_merchant`
- `checkout_blocked`
- `merchant_writeback_failed`
- `state_synced`

## Replay Rule

- event uniqueness is enforced by `checkout_id + event_type`
- replay must not create duplicate Shopify-forward events
- if a local order already exists and already carries `shopify_order_id`, replay resolves to `state_synced` without a second write-back call

## Reconciliation Note

- outward truth is journal-first
- local `orders` data remains the fallback evidence when reconstructing state
- transaction annotation is best-effort only when an external payment reference exists
