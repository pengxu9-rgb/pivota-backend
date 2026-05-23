# Legacy Phase 5.5/6 Commission System — Audit

**Date:** 2026-05-23
**Status:** **RESOLVED 2026-05-23 — full deprecation of the legacy system; v1.3 take-rate is the sole post-payment economic model going forward.** See §Resolution at the bottom for the decision rationale, the executed actions, and the remaining frontend follow-up.

User flagged that the original merchant→agent payout system, built ~2025-11 (Phase 5.5 / Phase 6 commits), runs in parallel with v1.3 monetization and could collide on the same orders. This document captures the audit findings so the design call has concrete numbers and code paths to reference. **No code or DB changes from this audit** — surface and document only.

## What exists

| Layer | Location | State |
|---|---|---|
| Tables | `merchant_commission_offers`, `agent_revenue_expectations`, `revenue_matching_logs`, `commissions` | Live in shared Postgres |
| Migration | `db/migrations/014_dual_sided_revenue.sql` (2025-11-03) | Applied |
| Services | `services/order_commission_service.py`, `services/revenue_share_service.py` | Live |
| Routes | `routes/merchant_commission_api.py` (mounted under `/merchants/{id}/commission`) | Live |
| Dashboard UI | `merchant.pivota.cc/dashboard/commission` | Live (frontend in `pivota-merchants-portal` repo, not this one) |

## Three trigger endpoints fire legacy commission

| Endpoint | File:Line | Who calls it | Fires commission? |
|---|---|---|---|
| `POST /payment/confirm` | `routes/order_routes.py:4334` | Merchant API direct call (X-Merchant-API-Key auth) | ✅ via `calculate_commission_task()` background |
| `POST /orders/{order_id}/confirm-payment` | `routes/agent_api.py:8689` | Agent API direct call | ✅ via `trigger_commission()` background (gated on `order.agent_id`) |
| `POST /admin/payouts/backfill` | `routes/admin_payout_backfill.py:47` | Admin manual trigger | ✅ synchronous for-loop over historical orders |

## Stripe webhook path does NOT fire legacy commission

`POST /webhooks/stripe/{psp_id}` → `services.psp_payment_finalizer.finalize_payment_success()` → mark order paid + sync to Wix/Shopify. **Zero commission logic anywhere in this chain.**

This explains the "0 commissions in last 30 days despite 15 paid agent orders" observation — all 15 are `ops_canary` Wix writeback canaries that arrived via Stripe webhook, bypassing the legacy commission path entirely.

## v1.3 monetization shape (for contrast)

| | Legacy (Phase 5.5/6) | v1.3 monetization |
|---|---|---|
| Charged to | Merchant pays agent directly | Merchant pays Pivota take rate |
| Rate | 1–5% (per merchant offer or platform default) | 10% standard / 5% promo |
| Trigger | `order_routes.py:4461` / `agent_api.py:8942` background tasks | T9 stamp on `finalize_payment_success` → T6 daily rollup → T7 monthly invoice |
| Records | `commissions` table | `commerce_attribution_edges` → `gmv_attribution_daily` |
| Pays out via | `agent_payouts` (`payee_type='agent'`) | `agent_payouts` (`payee_type='channel_partner'`) |

Both systems write to the same `agent_payouts` table but with different `payee_type` values — they can coexist at the table level. The economic collision is upstream of payouts.

## Quantified overlap with v1.3 Stage 2 backfill (alpha merchant)

| Metric | Value |
|---|---|
| Legacy commission rows in `commissions` (alpha) | 35 |
| Distinct legacy-commissioned orders for agent `agent_982b1ea2df866206` | 33 |
| Total legacy commission paid | $11.78 (avg ~1.3% effective rate, mostly `rate=0.0100`) |
| Date range of legacy commissions | 2025-11-27 → 2026-04-22 |
| Last 30 days | **0 new commissions** (all paid orders went via Stripe webhook path) |
| Stage 2 backfill candidates (agent orders, no v1.3 edge) | 80 |
| **Overlap: orders in BOTH legacy + Stage 2 set** | **30** |
| Gross GMV on overlap | $896.46 |
| v1.3 take rate (10%) on overlap | $89.65 |
| Legacy commission already paid on overlap | $11.51 |
| Combined merchant cost if Stage 2 runs as-is | $101.16 (~11.3% of GMV) |

## Merchant offers — note on provenance

The `merchant_commission_offers` rows for the alpha merchant were created by `system_migration` 2025-11-27, not by Chydan. The merchant never explicitly agreed to the 2.5% / 5% rates the offers encode. Disposition needs to acknowledge: these are migration defaults, not contracted commitments.

## Decision options (none chosen — deferred to Cowork)

1. **Pivota take-rate only (v1.3) — deprecate legacy entirely.** Hide dashboard, disable the 3 trigger endpoints from firing `process_order_commission`, archive `commissions` data, mark all `merchant_commission_offers` `is_active=false`. Markato + future merchants only see the v1.3 contract.

2. **Dual model — both systems coexist with explicit dual-pricing.** Keep both. Update merchant contracts so the combined cost is transparent. Requires UX work to reconcile the dashboards.

3. **Legacy is the real model — v1.3 take-rate is wrong.** Deprecate v1.3 T7 invoices, keep merchant→agent direct commission. Pivota earns via subscription only (T4 tiers). T7/T8 become dead code.

4. **Defer all action.** Acknowledge the collision exists; don't change either system; revisit before external merchant onboarding (e.g. before Markato signs).

## Pre-deciding-anything: actions needed regardless

These are operationally safe and don't pre-judge the architectural decision:

- [x] Document the audit (this file).
- [ ] **Confirm with Cowork** which model is the strategic intent.
- [ ] If Stage 2 backfill is going to run before the decision is made, **explicitly skip the 30 overlap orders** to avoid double-extraction. The Stage 2 script (`scripts/stage2_backfill_attribution_edges.py`) accepts a `--limit` flag but doesn't yet have a per-order exclusion list. Update to skip orders that already have a `commissions` row with matched=true.
- [ ] Once decision is made, update `docs/monetization/MERCHANT_ONBOARDING_READINESS.md` with the chosen model so merchant onboarding doesn't show conflicting commission UIs.

## Open questions for Cowork

1. When Phase 5.5/6 launched 2025-11, was the merchant→agent direct commission the **only** intended payout model? Was v1.3 already in design at that point?
2. The merchant.pivota.cc/dashboard/commission UI lets merchants set their own agent commission rates. Is that UX promise compatible with the "Pivota takes 10%" model?
3. The 30 alpha orders where both systems would extract — should the merchant be credited the legacy commission ($11.51) in their next v1.3 invoice if Stage 2 backfill includes them, or should those be skipped entirely from Stage 2?
4. The legacy system has no payout-execution layer (commissions table doesn't auto-trigger Stripe transfers). How were agents historically paid for these 35 commissions? Manual?

## Cross-references

- `docs/monetization/MERCHANT_ONBOARDING_READINESS.md` — the audit doc that prompted this investigation
- `docs/monetization/deploy/STAGE_2_HISTORICAL_BACKFILL.md` — Stage 2 plan; will need an additional design decision (§1.6) about the overlap orders once the legacy disposition is made
- `services/order_commission_service.py:process_order_commission` — the call entry point for legacy commission calculation
- `services/revenue_share_service.py:RevenueShareService.match_commission` — the rate-negotiation engine
- `routes/merchant_commission_api.py` — the merchant-facing API the dashboard calls

---

## Resolution (2026-05-23)

**Decision:** Drop the legacy Phase 5.5/6 merchant→agent direct commission system fully. v1.3 take-rate becomes the sole post-payment economic model. Pivota charges merchants a take rate (5%/10%) via T7 monthly Stripe invoices; partner shares are allocated via T8 Connect transfers; no merchant-set per-agent commission is collected or paid.

**Evidence base.** Codex industry research dispatched 2026-05-23 (full report at `/tmp/codex_monetization_research_output.md` — local artifact, ~28 platforms cited). Headline findings that drove the decision:

- **Comparable platforms converge on one economic clearing layer per order.** Faire (15% on Faire-sourced demand, 0% on merchant-direct), Substack/OnlyFans (single platform fee, no second commission), ChatGPT Instant Checkout (one "small fee" on completion, organic ranking), Apple/Google/Shopify App Stores (single tiered take rate). None of these maintains a parallel "merchant pays agent + platform pays itself" double-extraction.
- **Pivota's 5%/10% take rate is squarely in the defensible 5–10% range for AI-agent commerce** per codex's category analysis. No need to dilute it with a parallel commission system.
- **Faire's source-based model** (different rate by how the buyer was acquired) was the closest analog if a dual-rate structure was desired. But Cowork chose the cleaner "single rate, no dual model" path — fewer contractual surfaces, easier to explain to merchants.

**Actions taken (this resolution).**

| # | Action | Where | PR / commit |
|---|---|---|---|
| 1 | Removed `calculate_commission_task()` background dispatch from `/payment/confirm` | `routes/order_routes.py:~4442` | PR #612 |
| 2 | Removed `trigger_commission()` background dispatch from `/orders/{order_id}/confirm-payment` | `routes/agent_api.py:~8928` | PR #612 |
| 3 | `/admin/payouts/backfill` and `/admin/payouts/patch-schema` return HTTP 410 Gone | `routes/admin_payout_backfill.py` | PR #612 |
| 4 | `/merchants/{id}/commission/offers` (POST/GET/DELETE) all return HTTP 410 Gone | `routes/merchant_commission_api.py` | PR #612 |
| 5 | `UPDATE merchant_commission_offers SET is_active=false` (5 rows) with audit note | Production DB (direct psql via public proxy) | n/a — no code change |

**Actions deliberately NOT taken.**

- Service modules `services/order_commission_service.py` and `services/revenue_share_service.py` are **kept in the tree** for historical traceability of the existing `commissions` / `revenue_matching_logs` data. A future cleanup PR can remove them once we're confident nothing else in the codebase references them (already greppable: only the deprecated routes do).
- Historical data in `commissions`, `revenue_matching_logs`, `merchant_commission_offers`, `agent_revenue_expectations` is **not deleted**. The 35 commission rows + 5 offers are part of the alpha merchant's transaction history. Archival vs delete is a future call.
- The 30-overlap question for Stage 2 backfill is **moot post-deprecation**: legacy paid agent_982b... $11.51 historically (sunk cost). Stage 2 will create v1.3 attribution edges for all 80 qualifying alpha orders including those 30; T7 will charge the merchant 10% on the resulting net GMV with no offsetting credit. Since the alpha merchant is Chydan (the user), no external reconciliation needed. Document explicitly in Stage 2 runbook.

**Open follow-ups.**

1. **Frontend (different repo).** `merchant.pivota.cc/dashboard/commission` lives in `pivota-merchants-portal`, not this repo. UI clicks now return HTTP 410. Should be hidden in the merchant portal before any external merchant onboarding (Markato or other) to avoid showing a deprecated commission-config interface. **Tracking owner: needs assignment.**
2. **Stage 2 backfill runbook update.** Add a §1.6 to `docs/monetization/deploy/STAGE_2_HISTORICAL_BACKFILL.md` noting the legacy disposition and confirming all 80 orders are in scope (no skip list needed for the overlap).
3. **Service module cleanup.** Once one full Stage 4 monthly billing cycle has passed without incident, delete `services/order_commission_service.py`, `services/revenue_share_service.py`, and the `routes/admin_check_hidden_offers.py` / `routes/admin_fix_commission_offers.py` admin helpers (if they're also unused). Verify via grep that no other module imports them.
4. **Schema cleanup (defer to v1.5 or later).** `merchant_commission_offers`, `agent_revenue_expectations`, `revenue_matching_logs`, `commissions` tables can be marked DEPRECATED in COMMENT ON TABLE for now. Outright dropping needs a separate migration + backup, and is not urgent.

**Answers to the audit's open questions.**

1. *When Phase 5.5/6 launched 2025-11, was the merchant→agent direct commission the only intended payout model?* Per Cowork: yes — it predated the v1.3 design. v1.3 took shape ~2026-03/04 with a different economic model. The two never had a unified contract; the parallel-systems state is the result of evolution, not deliberate dual design.
2. *Is the dashboard UX promise compatible with "Pivota takes 10%"?* No. The dashboard implied merchants control their agent compensation; v1.3 implies Pivota does. The UI needs to go away (follow-up #1).
3. *Stage 2 overlap reconciliation?* See "Actions deliberately NOT taken" — moot under full deprecation; alpha = Chydan, no external counterparty.
4. *How were the historical 35 commissions paid out?* Unknown; the legacy system writes to `commissions` but has no Stripe-transfer execution layer. Likely manual ops if any payouts actually flowed. Future cleanup can confirm.
