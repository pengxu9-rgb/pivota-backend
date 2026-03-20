# Multi-Merchant External Referral Audit

## Audit Anchor

Audit date: `2026-03-20`

Merchant universe audited:

- source: production `GET /merchant/onboarding/all`
- authenticated through the live employee auth flow
- total live merchants returned: `17`

Runtime and operator surfaces verified in production:

- `GET /employee/referral-readiness/summary?merchant_id=...`
- `GET /employee/products/external-seeds?merchant_id=...&status=active&attached=true&limit=200`
- `POST https://agent.pivota.cc/api/gateway` with `operation=offers.resolve`

This audit was run after the following production changes were already live:

- backend referral governance and hard gating
- employee referral health and merchant-scoped seed tools
- backend attached-seed prefetch optimization for `offers.resolve`
- gateway-side `subject_resolve` skip for direct product-id referral lookups

## Executive Result

The strongest conclusion is:

`external referral is now runtime-healthy where seed coverage exists, but rollout remains red because merchant-level seed coverage is still sparse.`

That means the current external-referral bottleneck is no longer runtime correctness. It is coverage and merchant attribution.

## Merchant-Level Summary

Across all `17` live merchants:

- `1 / 17` merchants had any active referral seeds
- `1 / 17` merchants had any attached referral seeds
- `0 / 17` merchants had blocked referral seeds
- `0 / 17` merchants had review-only referral seeds

Referral status distribution from `GET /employee/referral-readiness/summary`:

- `green`: `1`
- `red`: `16`

Interpretation:

- the referral governance and summary system is functioning
- the runtime hard gate is not currently masking large numbers of bad seeds
- the dominant rollout problem is simply that most merchants do not yet have referral inventory

## Anchor Merchant: `merch_efbc46b4619cfbdf`

Production employee summary for the anchor merchant returned:

- `status=green`
- `gating_policy_version=external_referral_v1`
- `matched_domains=["jwx893-fz.myshopify.com"]`
- `total_active_seeds=50`
- `attached_seed_count=50`
- `healthy_seed_count=50`
- `blocked_seed_count=0`
- `review_seed_count=0`

This merchant is therefore no longer merely `referral-capable in theory`. It is `referral-healthy in production`.

## Runtime Probe Result For The Anchor Merchant

The anchor merchant's `50` attached referral products were re-probed against the live gateway:

- probe target: `https://agent.pivota.cc/api/gateway`
- operation: `offers.resolve`
- market: `EU-DE`
- input shape: direct `product_id`

Observed result:

- `50 / 50` returned `affiliate_outbound`
- `0` probe errors
- `0` runtime failure breakdown entries
- median total latency: `870.5ms`
- p95 total latency: `1574ms`
- median `time_to_pdp_ms`: `396ms`

Observed source markers:

- `subject_resolve:skipped_direct_lookup` -> `50`
- `cache_search` -> `50`

This means the anchor merchant's external-referral path is now both:

- operationally healthy
- materially faster than before the March 20 gateway optimization

## What This Audit Proves

### Proven

- employee-safe referral summary is live and usable across the merchant fleet
- hard-gated referral runtime is live
- the anchor merchant has real referral inventory and real outbound runtime coverage
- the previously observed `db_query_timeout` slow path is no longer present on the anchor merchant runtime audit
- the gateway no longer spends unnecessary time in `subject_resolve` for direct-id referral lookups

### Not Yet Proven

- multi-merchant referral coverage
- domain-match fallback quality for merchants without attached seeds
- merchant-facing referral diagnostics
- broad referral rollout readiness beyond the anchor merchant

## The Actual Bottleneck Now

The current external-referral constraint is:

`seed coverage and merchant attribution coverage`

It is no longer best described as:

- runtime instability
- broad blocker-grade seed corruption
- redirect path unreliability for the anchor merchant

The key unanswered rollout question is:

`how quickly can active merchants be given healthy attached or domain-matched referral inventory?`

## Recommended Next Steps

1. Backfill or attach referral seeds for the remaining live merchants before changing the rollout score.
2. Add a merchant-fleet coverage metric to employee ops:
   - merchants with active seeds
   - merchants with attached seeds
   - merchants with zero referral inventory
3. Extend the merchant readiness contract later with:
   - `external_referral_status`
   - `external_referral_issue_buckets`
   - `agent_surface_status`
4. Keep using runtime hard gating, but prioritize coverage expansion over further micro-optimizations.

## Updated Rollout Interpretation

As of `2026-03-20`:

- `system readiness` for external referral remains `Yellow`
  - because referral is live, governed, observable, and healthy where coverage exists
- `anchor merchant readiness` for external referral is now `Green`
  - because the anchor merchant has healthy attached seeds and `50 / 50` live runtime coverage
- `rollout readiness` for external referral remains `Red`
  - because only `1 / 17` live merchants currently has active attached referral inventory

This is the correct production interpretation after the March 20 verification pass.
