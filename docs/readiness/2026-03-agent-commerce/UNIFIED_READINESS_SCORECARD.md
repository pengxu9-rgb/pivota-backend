# Merchant Commerce vs Platform Fallback Readiness Scorecard

## Audit Anchor

- initial framing date: `2026-03-19`
- latest production rebaseline: `2026-03-20`
- anchor merchant: `merch_efbc46b4619cfbdf`

This scorecard replaces the older, misleading framing that treated `external referral` as if it were a merchant-owned readiness track.

The corrected model is:

1. `Merchant Commerce Readiness`
   - merchant-synced catalog
   - connected store/domain
   - PSP / checkout
   - real order and payment loop
2. `Platform Fallback Referral Readiness`
   - Pivota-managed external seeds
   - tracked redirects
   - outbound referral runtime
   - employee-owned audit and gating workflow

## Rules

- A merchant only counts as `ready` when real merchant commerce prerequisites are in place.
- External seeds do not count as merchant readiness.
- Seed attachment to a merchant/product graph is an operator/runtime linkage only:
  - routing
  - attribution
  - remediation scope
- There is no combined top-level score that treats `platform fallback` as equivalent to `merchant-valid commerce`.

## Scorecard

| Layer | Merchant Commerce Readiness | Platform Fallback Referral Readiness |
| --- | --- | --- |
| `System readiness` | `Green` - readiness snapshot, optimization plan, merchant workspace, employee ops, and one real merchant alpha for checkout/order/payment are live. | `Yellow` - employee-managed seed governance, redirect gating, runtime filtering, and fallback program observability are live, but fallback is still not a merchant-facing first-class contract. |
| `Merchant readiness` | `Yellow` - the live anchor merchant still shows `Needs Attention`, `score=77`, `2098 ready / 265 blocked`, with blocked variants still driven by `missing_price` and `out_of_stock`. | `Not applicable` - platform fallback is not a merchant-valid commerce dimension and must not be scored as merchant readiness. |
| `Rollout readiness` | `Yellow` - one real store-connected merchant is operational, but broader merchant-valid rollout remains unproven. | `Yellow` - the fallback program is operational and runtime-healthy where inventory exists, but it remains employee-operated fallback inventory rather than a merchant rollout KPI. |

## Why These Ratings Are Correct

### Merchant Commerce

Merchant commerce is the only valid top-level merchant readiness track:

- readiness diagnosis and scoring live in:
  - `readiness/models.py`
  - `readiness/scoring.py`
  - `readiness/summary.py`
- merchant-safe optimization is live in:
  - `routes/merchant_api_extensions.py`
  - `readiness/remediation.py`
- merchant validity still depends on:
  - synced catalog
  - store/domain connectivity
  - PSP / checkout capability
  - real order/payment loop

That is why the system is `Green`, while the anchor merchant remains `Yellow`.

### Platform Fallback Referral

Platform fallback is real, but it is not merchant-owned:

- external seeds are employee-uploaded and employee-governed
- redirect generation and outbound allowlist gating are live
- runtime hard blockers are enforced before fallback offers surface
- the fallback program now has employee-only summaries for:
  - per-merchant attached fallback seed health
  - program-level fallback inventory health
  - merchant commerce cohort background context

That is why fallback is `Yellow` at the system and rollout levels, but not scored as merchant readiness.

## Anchor Merchant Operational Note

The anchor merchant still has useful fallback telemetry, but it is operational only:

- attached fallback seed health:
  - `status=green`
  - `total_active_seeds=50`
  - `attached_seed_count=50`
  - `healthy_seed_count=50`
  - `blocked_seed_count=0`
  - `review_seed_count=0`
- live runtime audit:
  - `50 / 50` attached products returned `affiliate_outbound`
  - `0` runtime errors
  - median latency `870.5ms`
  - p95 latency `1574ms`

This proves the fallback program is healthy for that merchant graph. It does **not** upgrade the merchant itself to commerce-ready.

## Background Context Only

The following metrics remain useful for employee ops, but they are not top-level readiness denominators:

- total registered merchants
- test-account-heavy merchant lists
- merchant attachment counts for fallback seeds

Those are now treated as background or operator context only.

## Operational Separation

### Merchant-valid commerce

A merchant is only valid when all of these are true:

- connected store/domain
- synced active catalog in `products_cache`
- active PSP or internal checkout capability

### Platform fallback referral

Fallback program quality is measured by:

- seed audit health
- snapshot freshness
- redirect validity
- outbound allowlist validity
- runtime surfaced rate

### Attachment coverage

Attachment coverage remains a secondary metric only:

- useful for routing
- useful for attribution
- useful for employee remediation
- never used as the top-level merchant rollout denominator

## Final Verdict

As of `2026-03-20`:

- `Merchant Commerce Readiness`:
  - `system=Green`
  - `merchant=Yellow`
  - `rollout=Yellow`
- `Platform Fallback Referral Readiness`:
  - `system=Yellow`
  - `merchant=Not applicable`
  - `rollout=Yellow`

The correct production statement is:

`Merchant readiness means merchant-valid commerce only. External referral is a Pivota-managed fallback program, not a substitute for merchant commerce readiness.`
