# Real Merchant Alpha Report

## Merchant

- merchant id: `merch_efbc46b4619cfbdf`
- platform: Shopify
- alpha mode: `real_merchant_alpha`

## Current Alpha Readiness

Captured alpha expectation from the implemented test fixture:

- readiness score: `63`
- ready variants: `431000000001`, `431000000002`
- blocked variants: `431000000003`, `431000000004`
- checkout capability: `ready`
- order-sync capability: `ready`

## Primary Blockers Still Visible

- stale catalog/price/inventory on the IPSA variants
- normalized reviews/confidence still absent
- merchant-native payment execution still not implemented on the readiness router

## Evidence

- machine-readable summary fixture:
  - `readiness/fixtures/golden_real_merchant_readiness_report_ucp.json`
- machine-readable export summary:
  - `readiness/fixtures/golden_real_merchant_ucp_export.json`
- representative blocked checkout:
  - `readiness/fixtures/golden_real_merchant_blocked_checkout.json`
- representative successful order-sync:
  - `readiness/fixtures/golden_real_merchant_order_sync.json`

## Local Validation Caveat

This workspace did not have a live `DATABASE_URL` on March 17, 2026, so the alpha report above reflects the implemented readiness contract and captured merchant fixture, not a live local DB-backed merchant run.
