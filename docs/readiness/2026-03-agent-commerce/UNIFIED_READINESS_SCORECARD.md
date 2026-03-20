# Internal Commerce + External Referral Unified Readiness Scorecard

## Audit Anchor

Initial dual-track framing date: `2026-03-19`

Latest rollout verification update: `2026-03-20`

Anchor merchant:

- `merchant_id=merch_efbc46b4619cfbdf`
- platform: `Shopify`
- production backend build verified on March 19, 2026: `6a8b04bd1c5c6c92b42b5ef809ad05620cb5f71b`

This document upgrades the current readiness framing from a checkout-heavy alpha report into a dual-track scorecard that covers both agent-commerce surfaces supported by the live platform:

1. `Internal Commerce`
   - internal product
   - internal checkout
   - order, payment, refund, and return loop
2. `External Referral`
   - external seeds
   - no-checkout listings
   - tracked redirect and merchant-site handoff

Evidence used here is intentionally limited to:

- verified production facts observed on or before `2026-03-20`
- repo and runtime facts in the live backend codebase
- existing readiness alpha artifacts under `docs/readiness/2026-03-agent-commerce/`

No public API or portal behavior changes are introduced in this phase.

## March 20 Update

On `2026-03-20`, production was re-audited after external-referral governance, employee referral health surfaces, runtime hard gating, and two runtime latency fixes were already live.

The additional audit facts are:

- merchant universe checked: `17`
- merchants with any active referral seeds: `1`
- merchants with any attached referral seeds: `1`
- anchor merchant external-referral summary:
  - `status=green`
  - `total_active_seeds=50`
  - `attached_seed_count=50`
  - `healthy_seed_count=50`
  - `blocked_seed_count=0`
  - `review_seed_count=0`
- anchor merchant runtime probe:
  - `50 / 50` attached products returned `affiliate_outbound`
  - `0` runtime errors
  - median latency `870.5ms`
  - p95 latency `1574ms`

See `MULTI_MERCHANT_EXTERNAL_REFERRAL_AUDIT.md` for the full production readout.

## Unified Framing

The platform must treat two agent-compatible commerce surfaces as valid, but different:

- `checkout-ready` does not mean `all agent surfaces ready`
- `no internal checkout` does not automatically mean `not ready`

This scorecard therefore enforces two rules:

1. External offers without internal checkout are not auto-labeled `not ready` if redirect and landing quality are good enough for referral.
2. Referral-ready offers are never mislabeled as checkout-ready.

## Scorecard

| Layer | Internal Commerce | External Referral | Overall Agent Surface |
| --- | --- | --- | --- |
| `System readiness` | `Green` - first-class readiness snapshot, optimization plan, merchant workspace, employee ops, and live one-merchant checkout/order/refund/return alpha are in place. | `Yellow` - external seeds, tracked redirects, employee-safe referral health, and affiliate outbound runtime are live, but referral is still not yet a merchant-facing first-class readiness contract. | `Yellow` - both surfaces exist in production, but only internal commerce is fully modeled for merchants. |
| `Merchant readiness` | `Yellow` - live merchant portal still shows `Needs Attention`, `score=77`, `2098 ready / 265 blocked`; checkout, order-sync, and reviews are ready, but blocked variants remain dominated by `out_of_stock` and `missing_price`. | `Green` - the anchor merchant now has `50` healthy attached referral seeds, `0` blocked or review seeds, and `50 / 50` live runtime `affiliate_outbound` coverage in production. | `Yellow` - the anchor merchant is green for referral but still yellow overall because internal-commerce blockers remain. |
| `Rollout readiness` | `Yellow` - one-merchant production alpha is operational, but broader multi-merchant internal-commerce rollout is still unproven. | `Red` - the March 20 production audit found only `1 / 17` live merchants with any active attached referral inventory, so fleet coverage is still the dominant bottleneck. | `Red` - broad rollout should not claim unified agent-commerce readiness until referral coverage extends beyond the anchor merchant. |

## Why These Ratings Are Correct

### Internal Commerce

Internal commerce is already first-class in the live system:

- diagnosis and scoring live in `readiness/models.py`, `readiness/scoring.py`, and `readiness/summary.py`
- the merchant-safe optimization contract is live in `routes/merchant_api_extensions.py`
- the merchant-facing workspace is already built around `GET /merchant/readiness/optimization` plus `refresh / preview / run / jobs`
- the real merchant alpha has live production evidence for checkout, order-sync, payment-intent, payment-status-sync, refund, and return sync in `REAL_MERCHANT_ALPHA_REPORT.md`

This is why `system readiness` for internal commerce is `Green` even though the anchor merchant itself is still `Yellow`.

### External Referral

External referral is operationally present, and the anchor merchant is now runtime-healthy, but the fleet is still under-covered:

- employee-managed external seed curation exists under `routes/employee_products.py`
- employee-safe referral summary now exists through `GET /employee/referral-readiness/summary`
- tracked redirect generation exists in `_make_redirect_url()` in `routes/employee_products.py`
- agent runtime redirect and external-offer handling exists in:
  - `routes/agent_api.py`
  - `routes/agent_sdk_fixed.py`
  - `routes/agent_shop_gateway.py`
- the gateway explicitly emits `purchase_route=affiliate_outbound` and `affiliate_url` for external-seed offers
- the anchor merchant's attached referral products now return `affiliate_outbound` for `50 / 50` live probes
- the March 20 fleet audit found only `1 / 17` live merchants with any active attached referral inventory

What is now missing is primarily merchant coverage, plus a merchant-facing readiness contract for referral quality, freshness, landing integrity, and attribution. That is why referral is `Yellow` at the system level, `Green` for the anchor merchant, and still `Red` for broad rollout.

## Shared Blockers And Track-Specific Problems

### Shared blockers

These degrade both internal commerce and external referral:

- `missing_price`
- stale or wrong availability
- weak content structure
- missing or poor primary image

### Internal-only blockers

These specifically block internal commerce:

- missing checkout capability
- missing PSP readiness
- missing shipping or returns setup
- weak order, refund, or return operations

### External-only degraders

These specifically degrade referral:

- broken redirect or invalid destination
- stale external seed snapshot
- landing mismatch between seed and merchant page
- weak provenance or attribution

## Audit Inventory

### Internal Commerce Inventory

| Dimension | Source of truth / code | Live surface today | Signal availability | Current gap | Operator action path |
| --- | --- | --- | --- | --- | --- |
| catalog and readiness diagnosis | `readiness/scoring.py`, `readiness/summary.py` | `GET /merchant/readiness/optimization` | `Yes` | still centered on the current readiness taxonomy, not a unified dual-track model | merchant portal `/dashboard/product-optimization` |
| merchant-safe planning | `routes/merchant_api_extensions.py`, `readiness/remediation.py` | `refresh / preview / run / jobs` | `Yes` | non-content fixes still route to manual surfaces | merchant workspace and integrations |
| checkout and order loop | `readiness/service.py`, `readiness/order_sync.py`, `routes/readiness_internal.py` | internal alpha routes and live canaries | `Yes` | no universal readiness-owned PSP capture surface yet | internal ops, supervised alpha flows |
| payment, refund, return convergence | `REAL_MERCHANT_ALPHA_REPORT.md`, `STATUS.md` | live production canaries | `Yes` | still one-merchant alpha, not broad rollout | employee and internal ops |
| merchant and employee operational support | merchant portal and employee portal production surfaces | live production portal pages | `Yes` | still heavily internal-commerce focused | merchant workspace, employee merchant detail |

### External Referral Inventory

| Dimension | Source of truth / code | Live surface today | Signal availability | Current gap | Operator action path |
| --- | --- | --- | --- | --- | --- |
| seed storage and curation | `routes/employee_products.py` | `/employee/products/external-seeds*` | `Yes` | not projected into readiness scoring | employee seed tools |
| seed import and audit | `routes/employee_products.py` | `import-csv`, `preview`, `audit-queue`, `audit` | `Yes` | audit lives outside readiness control plane | employee seed workflow |
| redirect and handoff generation | `_make_redirect_url()` in `routes/employee_products.py`; `_make_external_redirect_url()` in `routes/agent_shop_gateway.py` | tracked redirect URLs and outbound actions | `Yes` | no redirect-integrity or landing-integrity score | runtime only; no merchant scorecard |
| agent retrieval and presentation | `routes/agent_api.py`, `routes/agent_sdk_fixed.py`, `routes/agent_shop_gateway.py` | external products, `external_redirect_url`, `affiliate_outbound` | `Yes` | no first-class referral readiness payload | runtime only |
| merchant portal support | none as a dedicated referral track | no explicit referral readiness page or bucket | `No` | biggest merchant-facing gap | explicit gap; no merchant self-serve path |
| employee operational support | employee seed curation and product linking | live employee seed tools | `Partial` | no unified referral dashboard or scorecard | employee-only operational workflow |

## Anchor Merchant Readout

### Internal commerce

Latest verified live merchant-facing summary for `merch_efbc46b4619cfbdf`:

- tier: `Needs Attention`
- readiness score: `77`
- ready variants: `2098`
- blocked variants: `265`
- dominant blockers: `out_of_stock`, `missing_price`
- checkout capability: `ready`
- order-sync capability: `ready`
- reviews/confidence capability: `ready`

This merchant is therefore `Yellow`, not `Red`, for internal commerce.

### External referral

For the same merchant, the latest production employee summary now shows:

- `status=green`
- `total_active_seeds=50`
- `attached_seed_count=50`
- `healthy_seed_count=50`
- `blocked_seed_count=0`
- `review_seed_count=0`

Live runtime probes against `https://agent.pivota.cc/api/gateway` also showed:

- `50 / 50` attached referral products returned `affiliate_outbound`
- `0` runtime errors
- `0` failure breakdowns

This merchant is therefore now `Green`, not `Yellow`, for external referral.

## Multi-Merchant Rollout Update

The `2026-03-20` production fleet audit changed the rollout interpretation:

- total live merchants checked: `17`
- merchants with any active referral inventory: `1`
- merchants with any attached referral inventory: `1`
- merchants with blocked referral seeds: `0`

So the external-referral rollout problem is no longer best described as runtime instability. It is better described as `merchant inventory coverage remains sparse outside the anchor merchant`.

## What This Audit Changes In The Framing

The current platform is no longer accurately described as only a checkout-readiness effort.

The more accurate production framing is:

- `Internal commerce readiness` is already first-class.
- `External referral readiness` is operationally present but under-modeled.
- `Overall agent surface readiness` is therefore broader than checkout readiness, but not yet fully normalized.

In practical terms:

- the merchant portal readiness workspace is currently strongest for internal-commerce blockers
- employee-operated external seed tooling already exists for referral
- the missing work is to bring referral into the same readiness language, scoring, diagnostics, and operator contract

## Next-Step Contract Additions

No public API changes are made in this audit phase.

The next contract additions should extend the existing readiness optimization payload instead of creating a separate referral API:

- `external_referral_status`
- `external_referral_issue_buckets`
- `agent_surface_status`
- `shared_vs_track_specific_blockers`

Recommended follow-on rule:

- extend `GET /merchant/readiness/optimization` with referral-aware fields
- do not create a parallel merchant referral score endpoint

## Final Verdict

As of `2026-03-20`:

- `Internal commerce readiness`: `system=Green`, `merchant=Yellow`, `rollout=Yellow`
- `External referral readiness`: `system=Yellow`, `merchant=Green`, `rollout=Red`
- `Overall agent surface readiness`: `system=Yellow`, `merchant=Yellow`, `rollout=Red`

The platform is now ready for a supervised one-merchant alpha across both internal checkout and external referral surfaces, but it is still not ready to claim broad unified readiness because referral coverage does not yet extend across the fleet.
