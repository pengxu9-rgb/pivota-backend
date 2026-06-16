# ADR-005: Audit Access & Credits Model

**Status:** Proposed (model owner-decided 2026-06-16; code reconciliation is follow-up) ·
**Scope:** `pivota-backend` (audit gating + credit ledger) + `pivota-merchants-portal` (provider UI, paywall copy)

---

## Context

The AI-audit product has two merchant-facing surfaces and a credit system, and the
**access model had drifted**: the code encodes an older *plan-tier feature-gate*
(free accounts run Gemini only, "at no charge"; `_maybe_premium_block(paid_tier)`
402s premium providers for free plans), while the intended model is **credit-metered**.
The drift caused repeated mis-derivation of "who can run what." This ADR is the single
source of truth so code, copy, and product decisions stop diverging.

The two surfaces (positioning is detailed separately; summarized here):
- **AI Visibility** (`/dashboard/agent-center/url-audit` → `POST /api/merchant-center/audit/url-readiness`):
  URL input, **no integration required**, can analyze competitors. The low-friction front door.
- **AI Readiness Audit** (`/dashboard/agent-center/ai-readiness` → `POST /api/audits`, per-SKU):
  **synced catalog**, deep per-SKU analysis (narrative, win-plan, GSC, indexing arc).

The two differ by **integration depth, not price or plan**.

---

## Decision — the model

### 1. Credit *balance* is the single gate
Every run — both pages, every provider — **debits credits**. A merchant can run anything
their balance covers. **Plan tier does not gate runs or providers.** The conceptually
correct gate everywhere is `balance ≥ cost(run)`.

### 2. The wallet has two funding sources, tracked as separate buckets
| Bucket | Granted by | Expiry |
|---|---|---|
| **Plan allotment** | A paid plan's monthly inclusion | **Expires monthly** (use-it-or-lose-it); **wiped on downgrade** to free |
| **Pay-as-you-go top-up** | One-time purchase (e.g. 500 credits), **no subscription required** | **Persists** — does not expire monthly; **survives downgrade** |

**Debit order:** spend the *expiring* plan allotment first, then the *persistent* top-up,
so a merchant never loses allotment they could have used. The ledger must track the two
buckets separately so one can expire without touching the other.

### 3. Premium providers are a cost multiplier, not a plan gate
ChatGPT/Claude **debit more credits per run** than Gemini (the cheaper baseline — **not**
free). Access = `balance ≥ premium cost`. There is **no plan requirement** for premium
providers.

### 4. The only credit-exempt path: the AI Visibility trial
New merchants get **2 free trial runs of AI Visibility** (URL flow, **up to 3 product URLs**
each, Gemini-grounded). After the 2 trial runs, Visibility costs credits like everything else.
This is the single free entry point; nothing else is free.

### 5. Re-up paths when plan allotment is exhausted
Two paths, both crediting the same balance: **upgrade tier** (more monthly allotment) **or**
**top-up** (one-time purchase, no subscription). Top-up is the lower-friction conversion.

### Tier matrix
| | Free plan | Paid plan |
|---|---|---|
| Plan allotment | none | monthly (expires) |
| Can buy top-ups | **yes** (no subscription) | yes |
| AI Visibility | 2 free trial runs, then needs credits | needs credits |
| AI Readiness | needs credits (+ synced catalog) | needs credits (+ synced catalog) |
| Premium providers | if balance covers the cost | if balance covers the cost |

---

## Consequences

- **"Free plan + credits" is a valid state** — funded by a top-up. Therefore **plan tier is
  NOT a valid proxy for "can run premium."** A free-plan merchant who bought credits must be
  allowed to spend them on any provider.
- **Conversion has two low-friction paths**, not one: *subscribe* (recurring) **or** *top-up*
  (one-time). The post-Visibility "run deeper analysis →" CTA should offer the top-up path so a
  merchant can run the deep audit without committing to a subscription.
- **One currency to explain** — no free-vs-paid feature matrix. Plans = credit allotment +
  pricing, not access. This is the PLG-friendly story.
- **History + re-audit** (both flows persist results) is what drives recurring credit spend; it
  is the retention engine that justifies the model.

---

## Reconciliation — code divergences to fix (follow-up work)

The code currently implements the *old plan-tier model*. To conform to this ADR:

1. **`routes/audit_runs_routes.py:411` `_maybe_premium_block(paid_tier=…)`** — replace the
   `paid_tier` plan-gate with **credit-balance sufficiency** (premium = higher cost). Drop the
   402 `premium_provider_subscription_required` plan paywall; insufficient balance (the existing
   `previewSufficient` / insufficient-credits path) is the correct refusal.
2. **The 402 copy** — *"free accounts can run Gemini audits at no charge"* — remove; Gemini costs
   credits.
3. **Portal** — `PremiumProviderRequiredBanner`, the provider-selector plan gating, and the
   "FREE/PREMIUM" badges (partly addressed by portal PR #60) — reframe premium as **cost**, not
   plan; surface per-provider credit cost rather than a subscribe wall.
4. **Credit ledger** — ensure two buckets (plan-allotment-expiring vs top-up-persistent) with the
   expiry rules and debit order above; downgrade wipes allotment only.
5. **URL count** — AI Visibility allows "up to 5 product URLs" in code; this model says **3** —
   align.

---

## Open questions (out of scope here)

- **Credit pricing per provider** — the actual cost multipliers (Gemini vs ChatGPT/Claude) and
  per-run/per-SKU credit prices.
- **Pre-signup AI Visibility** — whether the URL flow is a public/PLG lead magnet (runnable before
  becoming a Pivota merchant) vs login-gated. A separate positioning decision with large
  acquisition implications.
- **Unified run-history UI** for both flows (the retention surface) — separate build; the data is
  largely persisted already (`merchant_audit_runs.report_jsonb`; `getAuditRunDetail` is the
  per-run view primitive).
