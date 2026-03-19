# Internal Commerce + External Referral Unified Readiness Scorecard

## Audit Anchor

Audit date: `2026-03-19`

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

- verified production facts observed on or before `2026-03-19`
- repo and runtime facts in the live backend codebase
- existing readiness alpha artifacts under `docs/readiness/2026-03-agent-commerce/`

No public API or portal behavior changes are introduced in this phase.

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
| `System readiness` | `Green` - first-class readiness snapshot, optimization plan, merchant workspace, employee ops, and live one-merchant checkout/order/refund/return alpha are in place. | `Yellow` - external seeds, tracked redirects, and affiliate outbound runtime are live, but referral is not yet a first-class readiness contract. | `Yellow` - both surfaces exist in production, but only internal commerce is fully modeled and operatorized. |
| `Merchant readiness` | `Yellow` - live merchant portal still shows `Needs Attention`, `score=77`, `2098 ready / 265 blocked`; checkout, order-sync, and reviews are ready, but blocked variants remain dominated by `out_of_stock` and `missing_price`. | `Yellow` - the merchant can be represented through referral-capable runtime surfaces, but no dedicated referral score, redirect-health score, or seed-health diagnostics exist for this merchant today. | `Yellow` - the merchant is usable in alpha, but is not green across both transaction and referral surfaces. |
| `Rollout readiness` | `Yellow` - one-merchant production alpha is operational, but broader multi-merchant internal-commerce rollout is still unproven. | `Red` - referral runtime exists, but seed freshness, redirect integrity, landing integrity, and attribution are not yet normalized into a shared readiness contract. | `Red` - broad rollout should not claim unified agent-commerce readiness until referral becomes first-class. |

## Why These Ratings Are Correct

### Internal Commerce

Internal commerce is already first-class in the live system:

- diagnosis and scoring live in `readiness/models.py`, `readiness/scoring.py`, and `readiness/summary.py`
- the merchant-safe optimization contract is live in `routes/merchant_api_extensions.py`
- the merchant-facing workspace is already built around `GET /merchant/readiness/optimization` plus `refresh / preview / run / jobs`
- the real merchant alpha has live production evidence for checkout, order-sync, payment-intent, payment-status-sync, refund, and return sync in `REAL_MERCHANT_ALPHA_REPORT.md`

This is why `system readiness` for internal commerce is `Green` even though the anchor merchant itself is still `Yellow`.

### External Referral

External referral is operationally present, but still under-modeled:

- employee-managed external seed curation exists under `routes/employee_products.py`
- tracked redirect generation exists in `_make_redirect_url()` in `routes/employee_products.py`
- agent runtime redirect and external-offer handling exists in:
  - `routes/agent_api.py`
  - `routes/agent_sdk_fixed.py`
  - `routes/agent_shop_gateway.py`
- the gateway explicitly emits `purchase_route=affiliate_outbound` and `affiliate_url` for external-seed offers

What is missing is not runtime capability. What is missing is a first-class readiness contract for referral quality, freshness, landing integrity, and attribution. That is why referral is `Yellow` at the system level and `Red` for broad rollout.

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

For the same merchant, external-referral readiness cannot yet be scored from a dedicated contract:

- the live platform can represent referral-capable offers through external seeds and tracked redirects
- the runtime can emit outbound purchase routes
- but the merchant does not yet receive a first-class referral score, referral issue buckets, or redirect/landing health diagnostics

This merchant is therefore `Yellow`, not `Green`, for external referral. The system has the runtime pieces, but not a merchant-safe referral readiness model.

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

As of `2026-03-19`:

- `Internal commerce readiness`: `system=Green`, `merchant=Yellow`, `rollout=Yellow`
- `External referral readiness`: `system=Yellow`, `merchant=Yellow`, `rollout=Red`
- `Overall agent surface readiness`: `system=Yellow`, `merchant=Yellow`, `rollout=Red`

The platform is ready for a supervised one-merchant alpha across agent-compatible commerce surfaces, but it is not yet ready to claim broad unified readiness across both internal checkout and external referral.
