# Credit-Gate Reconciliation — Execution Plan

**Status:** Ready to execute (gated on the GitHub Actions billing fix; needs two product decisions before Phases 3 / Track B) · **Date:** 2026-06-16
**Implements:** [ADR-005](adr/ADR-005-audit-access-and-credits-model.md). **Corrects** ADR-005's "Reconciliation appendix," which mis-sized the work.

---

## The key correction

ADR-005's appendix assumed the two-bucket wallet had to be *built*. A code map of the current system shows **most of it already exists and is wired**:

| ADR-005 requirement | Reality today |
|---|---|
| Wallet = plan-allotment (expires) + top-up (persists), tracked separately | **Built** — `merchant_credit_balance.allowance_credits` + `allowance_period_start` (expiring) and `purchased_credits` (persistent) (`migrations/091`, `migrations/141`) |
| Debit expiring allotment first | **Built** — `merchant_credit_balance_service.py:732-737` |
| Pay-as-you-go top-up (no subscription) → persists | **Built backend** — `POST /api/credits/topup` → Stripe PI → webhook → `_apply_credit_topup` adds to `purchased_credits` (`billing_routes.py:406-431`, `merchant_credit_balance_service.py:1227-1345`) |
| Premium = cost multiplier, not a plan gate | **Cost side already true** — per-provider token pricing (`config/provider_credit_rates.json`: Gemini $0.30/$2.50 vs ChatGPT $5/$30 vs Claude $3/$15 per 1M); a premium-inclusive run already debits more (`provider_credit_rates.py:100-140`, `audit_runs_routes.py:576-594`) |

**So the reconciliation is mostly *subtractive* — delete a plan-gate that sits on top of an already-correct credit system — plus one new portal button, two bug fixes, and (separately) one genuine architecture project.**

---

## What's actually wrong (the gaps)

1. **Premium providers are plan-gated.** `_maybe_premium_block(paid_tier)` raises **402 `premium_provider_subscription_required`** when a free-plan account requests ChatGPT/Claude (`audit_runs_routes.py:411-444`, called `:995-1001`). Contradicts "balance is the single gate" — a free-plan merchant with top-up credits *can* pay for premium but is blocked.
2. **The balance gate only applies to free tier.** Paid tiers bypass `balance ≥ cost` and accrue **overage** (`merchant_credit_balance_service.py:724-731`; `audit_runs_routes.py:1029,1044`). ADR-005 says balance gates *everyone*.
3. **Downgrade doesn't wipe the allowance.** `_downgrade_merchant_to_free` only updates `merchants.current_tier`/`subscription_id` (`billing_routes.py:1196-1219`); `merchant_credit_balance.allowance_credits` is left intact and `plan_tier` goes **stale** (the lazy reset no-ops once there's no active subscription). ADR-005 §2 requires "wiped on downgrade."
4. **No portal top-up surface.** The `POST /api/credits/topup` backend exists but **no merchant UI calls it** (grep finds none). ADR-005 wants the post-Visibility CTA to offer it.
5. **URL count is 5, should be 3.** `MerchantUrlAuditRequest.product_urls max_length=5` (`merchant_audit_routes.py:1009`) + copy.
6. **Two parallel credit ledgers.** Stripe checkout writes `merchant_credits` + `credit_ledger` (`billing_routes.py:526-588`); the audit path reads `merchant_credit_balance` (allowance derived lazily, never written by checkout). They can disagree. The only genuine architecture project.
7. **Stale copy/refs.** "Gemini at no charge" in the 402 (`audit_runs_routes.py:438-440`) and portal (`page.tsx:1007`); `coverage_profiles.py:65-69` references the deleted `services/audit_entitlements.py`.

---

## Decisions required (product, before Phases 3 / Track B)

- **D1 — Paid-tier overage: keep or kill?** Phase 3 (#2) makes `balance ≥ cost` gate paid tiers too. Taken literally, that **removes the overage subsystem** (`overage_pending_credits`, the sweep job, Stripe overage charges). If overage should survive for paid tiers, ADR-005 needs an explicit exception ("paid tiers may overspend into metered overage"). **This is a billing-model decision, not an engineering one.**
- **D2 — Canonical ledger (Track B).** Which store is the source of truth — `merchant_credit_balance` (what audits read) or `merchant_credits`/`credit_ledger` (what checkout writes)? Recommend `merchant_credit_balance` (it already has buckets + expiry + top-up), backfilled from the ledger. Needs confirmation + a data check (below).
- **D3 — Premium pricing.** Per-provider COGS pricing is already live; confirm the resulting credit costs are the intended merchant-facing prices (no separate "premium multiplier" knob is needed unless you want margin on top of COGS).

---

## Phased plan

### Phase 1 — Delete the plan-gate (small · low-risk · highest signal)
Makes premium credit-gated, not plan-gated — the core ADR-005 fix. Purely subtractive.

- **Backend:**
  - Remove `_maybe_premium_block` + its call + the 402 `premium_provider_subscription_required` (`audit_runs_routes.py:411-444`, `:995-1001`). Premium is now gated only by balance (the existing insufficient-credits path) and its already-higher per-provider cost.
  - Delete the "free accounts can run Gemini audits at no charge" copy (`:438-440`).
  - Decide the `if paid_tier:` verified-card requirement at `:1013-1027` — keep it as a *top-up/charge* prerequisite (re-expressed as "needs a payment method to buy credits") or drop it. Not a blocker for Phase 1; flag.
  - Fix stale ref in `coverage_profiles.py:65-69`.
  - **URL 5→3** (`merchant_audit_routes.py:1009` + copy `:1011,1160`).
- **Portal:** delete `PremiumProviderRequiredBanner` (`page.tsx:991-1019`), the Premium/Free plan badges + `isFree` branches (`ProviderSelector` `:918-988`), and the `plan_tier === 'free'` copy (`:655-665`); reframe the provider chips as **per-provider credit cost** in the cost preview. Drop the stale "Gemini at no charge" (`:1007`). Remove the now-dead `PremiumProviderRequiredError` path (`api-client.ts:1888`, `credit-errors.ts:43-62`). (Builds on the badge copy already shipped in portal #60.)
- **Tests:** free-plan + premium provider + **sufficient balance → runs** (no 402); free-plan + premium + **insufficient balance → 402 insufficient_credits** (not subscription).
- **Risk:** LOW. Behavioral change = free-plan-with-credits can now run premium (intended).

### Phase 2 — Downgrade wipes allowance + portal top-up CTA (small-medium)
- **Backend (#3):** in `_downgrade_merchant_to_free` (`billing_routes.py:1196-1219`), also zero `merchant_credit_balance.allowance_credits` and set `plan_tier='free'` (preserve `purchased_credits`). Fixes the stale-allowance + stale-`plan_tier` bug (which also feeds `paid_tier`). Test: downgrade zeros allowance, keeps purchased, flips plan_tier.
- **Portal (#4):** build the "Buy credits" UI calling `POST /api/credits/topup` (backend already complete). Wire it into the **post-Visibility "run deeper analysis →"** CTA so the conversion offers the top-up path (ADR-005). UI-only.
- **Risk:** LOW-MEDIUM (the downgrade change touches billing webhooks — test the Stripe handlers).

### Phase 3 — Balance gate applies to paid tiers (medium · billing-sensitive · needs D1)
- **Blocked on D1.** If "kill overage": remove the `paid_tier` bypass in `_apply_delta` (`merchant_credit_balance_service.py:724-731`) and the `not paid_tier`-gated 402s (`audit_runs_routes.py:1029,1044`; `merchant_audit_routes.py:1231`), so every run requires `balance ≥ cost`; then retire the overage subsystem (`overage_*` columns, sweep, Stripe overage charges). If "keep overage": amend ADR-005 instead and scope this down to copy.
- **Risk:** MEDIUM-HIGH (live billing/overage). Do last, with billing sign-off.

### Track B — Two-ledger consolidation (large · own project · needs D2 + data check)
- **Pre-req runtime check (do first):** the "two ledgers diverge" claim is static-analysis-inferred. Query prod: for active merchants, compare `merchant_credits.balance` vs `merchant_credit_balance.credits` (and the allowance/purchased split). If they already track (a trigger/sync I didn't find), this track shrinks dramatically.
- If they diverge: pick canonical (D2 → recommend `merchant_credit_balance`), backfill from the other, repoint all readers/writers (checkout `billing_routes.py:526-588`), and remove the dead store.
- **Risk:** HIGH (subsystem-scale). Independent of Phases 1-3.

---

## Sequencing & gating

1. **Now (unblock):** fix the **GitHub Actions billing/spending-limit** (Settings → Billing & plans) so backend CI runs; everything below is backend.
2. **Run the Track-B data check** (read-only; cheap; resizes the biggest unknown).
3. **Phase 1** → **Phase 2** (high-value, low-risk, mostly subtractive + one button).
4. **Decide D1**, then **Phase 3**.
5. **Decide D2**, then **Track B** as its own effort.

**Net:** the visible ADR-005 surface (premium no longer needs a plan; top-up reachable; honest copy; URL=3) is reachable in **Phases 1-2 — small, low-risk** — because the credit plumbing already exists. The hard parts (D1 overage, Track-B ledger) are isolated and decision-gated, not prerequisites.
