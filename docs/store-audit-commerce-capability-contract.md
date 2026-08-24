# Store Audit → Commerce Index capability contract

This lane records whether a merchant storefront can be routed to safely. It is
not a payment authorization or order-creation workflow.

## Granularity

- `commerce_checkout_route` is a **merchant-level** fact. A single
  representative product may be used to reach the route, but the result is
  reused for that merchant until its 24-hour expiry.
- `commerce_cartability` is a **SKU-level** fact. It is short-lived (6 hours)
  and only states whether that selected SKU was added to an anonymous cart.
- `commerce_platform` is merchant-level and expires after 30 days.

The scheduler selects at most one active `storefront` route for each merchant
and uses a merchant/day idempotency key. It does not schedule checkout work per
SKU.

## Receipt boundary

The browser worker may submit only enumerated fields:

- platform and checkout-provider labels;
- cart status, quantity, price, currency;
- checkout-route status and an optional challenge stage;
- terminal outcome codes.

It must never send a browser URL with query data, page text, cookies, session
tokens, buyer identity/address data, payment credentials, payment tokens, or
an order identifier. Successful probes must include a checkout-route result.

## Capability resolution

| Evidence state | Agent route policy | Payment capability |
| --- | --- | --- |
| No current checkout evidence | `discovery_only` | `unverified` |
| Guest checkout route found | `merchant_handoff` | `unverified` |
| WAF/security challenge before address | `user_takeover_required` | `unverified` |
| Explicit merchant-authorized integration | `agent_checkout_eligible` | `merchant_authorized_revalidation_required` |

Cart success never promotes a merchant to agent checkout. Even an
`agent_checkout_eligible` merchant must obtain live shipping, tax, price, and
inventory validation before an order or payment is considered.
