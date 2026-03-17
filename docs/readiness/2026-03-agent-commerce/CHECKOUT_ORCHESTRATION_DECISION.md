# Checkout Orchestration Decision

## Chosen Path

- external alpha surface: `routes/readiness_internal.py`
- orchestration owner: `readiness/service.py`
- session/event store: `readiness/order_sync.py`

## Why This Path

- keeps the readiness API stable
- avoids exposing legacy order/payment route fragmentation
- reuses local order persistence and Shopify write-back primitives instead of inventing a second order stack

## Flow

1. readiness report decides whether a variant is checkout-ready
2. readiness checkout creates an idempotent readiness checkout session
3. order-sync verifies current merchant capability
4. order-sync creates a local order if buyer context exists
5. order-sync forwards to Shopify when merchant connection is available
6. order-sync marks outward state through the readiness journal

## Explicit Non-Scope In This Phase

- full PSP payment execution on the readiness router
- webhook-driven payment confirmation on the readiness router
- multi-merchant or multi-platform checkout orchestration
