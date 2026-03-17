# Execution Charter

## Scope

- turn the readiness thin slice into a one-merchant alpha for `merch_efbc46b4619cfbdf`
- replace the synthetic-only source path with a real Shopify merchant adapter
- preserve the existing readiness endpoint surface where possible
- define canonical source-of-truth rules for:
  - catalog/title/description/media
  - price/currency
  - inventory/availability
  - fulfillment/returns policy
  - checkout capability
  - order status
- converge checkout/order-sync onto one readiness-owned canonical path
- add diagnostics, fixtures, tests, and operator docs

## Non-Goals

- broad multi-merchant rollout
- Google Merchant Center implementation
- ChatGPT ACP or product-feed production claims
- ads / GEO / AEO implementation
- UI-heavy work beyond internal diagnostics
- broad refactors outside readiness-critical paths

## Execution Phases

1. Contract lift
   - extend readiness models, flags, and router/service seams
2. Real merchant source
   - add Shopify live adapter for the alpha merchant
3. Canonical truth
   - implement source-of-truth policy engine and surface it in scoring/report/export
4. Canonical execution
   - route checkout/order-sync through one readiness-owned orchestration path
5. Diagnostics and runbooks
   - add sample artifacts, summary docs, and developer instructions

## Success Criteria

- `GET /internal/readiness/merchants/{merchant_id}/report?channel=ucp` works for `merch_efbc46b4619cfbdf`
- `GET /internal/readiness/merchants/{merchant_id}/exports/ucp` returns a real-merchant UCP-style export
- `POST /internal/readiness/merchants/{merchant_id}/checkout` uses the readiness-owned canonical flow
- `POST /internal/readiness/merchants/{merchant_id}/order-sync/{checkout_id}` replays safely and does not create duplicate merchant-forward events
- all new behavior remains additive and feature-flagged
- synthetic readiness fixtures/tests remain intact

## Rollback Constraints

- rollback must be flag-based first
- do not remove or rewrite legacy `order_routes.py`
- do not remove the synthetic readiness path
- do not require schema migration rollback to disable the alpha

## Feature Flags

- existing:
  - `FEATURE_READINESS_AUDIT`
  - `FEATURE_READINESS_UCP_THIN_SLICE`
- new:
  - `FEATURE_READINESS_REAL_MERCHANT_ALPHA`
  - `FEATURE_READINESS_SOURCE_OF_TRUTH_V1`
  - `FEATURE_READINESS_CANONICAL_CHECKOUT_ALPHA`
  - `READINESS_ALPHA_MERCHANT_ID=merch_efbc46b4619cfbdf`
