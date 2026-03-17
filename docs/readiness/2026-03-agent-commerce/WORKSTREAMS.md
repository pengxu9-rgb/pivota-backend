# Workstreams

## WS1 Contract and Flags

- extended readiness models with merchant alpha mode, capability status, blockers, warnings, and field-family source-of-truth status
- added real-merchant alpha flags and alpha merchant selector

## WS2 Real Merchant Adapter

- added `readiness/sources/shopify_live.py`
- merchant restricted to `merch_efbc46b4619cfbdf`
- cache-first, live-Shopify fallback

## WS3 Source Of Truth

- added `readiness/source_of_truth.py`
- encoded canonical, fallback, freshness, degradation, and blocker semantics for six field families

## WS4 Canonical Checkout / Order Sync

- readiness service now owns checkout session creation
- readiness service now owns canonical order-sync advancement for the real merchant alpha
- journal extended with generic event append and session update operations

## WS5 Diagnostics and Validation

- report/export responses now include capability, freshness, provenance, blockers, and warnings
- added captured real-merchant fixtures
- added readiness regression tests
