# Platform Fallback Referral Program Audit

## Audit Anchor

- audit date: `2026-03-20`
- authenticated through the live employee flow
- anchor merchant: `merch_efbc46b4619cfbdf`

Verified production surfaces:

- `GET /employee/referral-readiness/summary?merchant_id=...`
- `GET /employee/referral-readiness/program-summary`
- `GET /employee/referral-readiness/merchant-commerce-cohort`
- `GET /employee/products/external-seeds?merchant_id=...&status=active&attached=true&limit=200`
- `POST https://agent.pivota.cc/api/gateway` with `operation=offers.resolve`

## Subject Correction

This audit no longer treats external referral as merchant-owned readiness.

The correct subject is:

- `platform fallback referral program`

The wrong subject was:

- `merchant fleet external referral coverage`

External seeds are employee-uploaded and employee-governed fallback inventory. Attachment to a merchant/product graph is useful for runtime routing and operator remediation, but it is not merchant ownership and it is not merchant-valid commerce readiness.

## Important Denominator Correction

The production `merchant/onboarding` list contains many test or non-production merchants.

Per product truth:

- only merchants with real synced catalog + connected store/domain + PSP/checkout count toward merchant-valid commerce
- external fallback program health should be measured against active seed inventory and runtime quality, not against all registered merchants

So metrics like `1 / 17 merchants covered` are background context only. They are not the primary denominator for fallback program readiness.

## Program-Level Result

The strongest current conclusion is:

`platform fallback referral is runtime-healthy and governed where inventory exists, and should now be measured as a program, not as merchant readiness.`

What is now proven:

- employee-safe attached fallback seed health is live
- employee-safe program summary is live
- runtime hard gating is live
- redirect allowlist enforcement is live
- the anchor merchant graph has healthy attached fallback inventory

## Anchor Merchant Operational Readout

For `merch_efbc46b4619cfbdf`, production showed:

- `status=green`
- `total_active_seeds=50`
- `attached_seed_count=50`
- `healthy_seed_count=50`
- `blocked_seed_count=0`
- `review_seed_count=0`

This is an operator/debug confirmation that the attached fallback inventory is healthy for that merchant graph.

It is **not** a statement that the merchant is fallback-ready as a business-valid commerce path.

## Runtime Probe Result

The anchor merchant's attached fallback products were re-probed against the live gateway:

- `50 / 50` returned `affiliate_outbound`
- `0` runtime errors
- `0` failure breakdown entries
- median total latency: `870.5ms`
- p95 total latency: `1574ms`
- median `time_to_pdp_ms`: `396ms`

Observed source markers:

- `subject_resolve:skipped_direct_lookup`
- `cache_search:ok`

This proves the fallback runtime path is operationally healthy where attached inventory exists.

## Merchant Commerce Cohort Context

A separate employee-only cohort view now exists for merchant-valid commerce prerequisites:

- `total_registered_merchants`
- `store_connected_merchants`
- `store_connected_with_psp_merchants`
- `merchant_valid_count`
- `merchant_invalid_count`

This is intentionally separate from fallback program health.

It exists to answer:

- which merchants are real commerce candidates
- which merchants still fail basic commerce prerequisites

It does **not** define fallback program readiness.

## What This Audit Proves

### Proven

- fallback governance is live
- fallback runtime gating is live
- fallback runtime latency is materially improved
- attached fallback inventory for the anchor merchant is healthy
- employee ops now has the right split:
  - `Attached Fallback Seed Health`
  - `Platform Fallback Referral Program`
  - `Merchant Commerce Cohort`

### Not Proven

- broad merchant-valid rollout beyond the one real connected merchant
- a merchant-facing fallback contract
- that fallback should be exposed to merchants as a self-serve readiness surface

## Correct Interpretation

As of `2026-03-20`:

- fallback is no longer best described as `merchant coverage is sparse`
- fallback is better described as:
  - a `Pivota-managed fallback program`
  - with healthy runtime where inventory exists
  - still employee-operated
  - still not equivalent to merchant-valid commerce

## Recommended Next Steps

1. Keep fallback program reporting separate from merchant-valid readiness.
2. Keep per-merchant attached fallback health as an operator/debug surface only.
3. Do not add merchant-facing fallback readiness until there is an explicit product decision for how fallback should be presented.
4. Continue improving merchant-valid commerce separately:
   - store/domain connectivity
   - catalog sync
   - PSP/checkout connection

## Final Audit Verdict

`Platform fallback referral is now a governed runtime program, not a merchant readiness track.`
