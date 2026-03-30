# Celestial Pivot Follow-On Phases

## Summary
The current release phase is closed when all of the following are true:
- beauty ranking parity is green
- serve canary is green for `shopping_agent`, `shopping-agent-ui`, and `shopping-agent-web`
- direct commerce channels signoff is green for one approved primary merchant

The next level up should not be folded into the current close-out.
Treat these as separate phases with separate success criteria:
- Phase A: multi-merchant expanded acceptance
- Phase B: real payment completion path

Recommended order:
1. Phase A first
2. Phase B second

## Current Status
Current status as of `2026-03-30 UTC`:

- `Phase A` current-environment gate is complete.
  - Evidence:
    - [commerce-signoff-batch.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/commerce-signoff/prod-batch-20260329-current-gate/commerce-signoff-batch.md)
    - [commerce-signoff-batch.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/commerce-signoff/prod-batch-20260329-current-gate/commerce-signoff-batch.json)
  - Current-environment outcome:
    - `overall_ok = true`
    - `enabled_cases = 1`
    - `passed_cases = 1`
    - current gate requires only `beauty`
  - Long-term expansion is still intentionally open:
    - `target_enabled_cases = 5`
    - `target_semantic_classes = ["beauty", "generic_default", "fragrance"]`

- `Phase B` supervised real payment completion is complete.
  - Evidence:
    - [bridge-paid-reference-signoff.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/phase-b-signoff/prod-bridge-paid-reference-complete-20260330T003949Z/bridge-paid-reference-signoff.md)
    - [bridge-paid-reference-signoff.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/output/phase-b-signoff/prod-bridge-paid-reference-complete-20260330T003949Z/bridge-paid-reference-signoff.json)
  - Outcome:
    - a fresh readiness-owned Stripe Checkout session reached a real paid terminal state
    - readiness verified the paid PSP state before bridging it into the local order
    - `payment-bridge` converged successfully
    - `refund` converged successfully
    - post-refund audit ended at `payment_status=refunded`
    - `overall_ok = true`

Net result:
- both follow-on phases now have a green minimum slice
- the remaining work is expansion and repeatability, not first-path feasibility

## Phase A: Multi-Merchant Expanded Acceptance

### Goal
Move from one approved primary-merchant signoff to a representative merchant cohort.

### Why This Is A Separate Phase
The current signoff proves production health for one real merchant and one safe order-backed payment initiation path.
That is enough to close the current phase, but it does not prove that the same behavior is stable across multiple merchants, catalogs, or PSP mixes.

### Scope
- keep the same production-safe channels:
  - catalog read-side query
  - catalog write-side webhook + sync job + backfill apply/verify
  - order-backed payment initiation canary
- expand from one merchant to a small curated cohort
- keep this phase non-paid and non-capture

### Merchant Cohort Rules
- current production minimum gate:
  - at least 1 live-eligible merchant
  - include `beauty`
- long-term target:
  - at least 5 merchants
  - include `beauty`, `fragrance`, and generic commerce coverage
- include more than one PSP readiness profile
- include at least one merchant with a larger catalog
- include at least one non-EUR pricing case

### Deliverables
- merchant cohort manifest, for example `merchant_id`, `label`, `query`, `expected_psp`
- per-merchant JSON/Markdown signoff artifacts
- one rollup summary with:
  - pass/fail by merchant
  - channel-level failures
  - backfill duration distribution
  - payment canary PSP/provider distribution

### Suggested Tooling Work
- batch wrapper: [run_commerce_channels_signoff_batch.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/run_commerce_channels_signoff_batch.py)
- merchant cohort fixture: [commerce_signoff_cohort.json](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/fixtures/commerce_signoff_cohort.json)
- direct single-merchant signoff: [smoke_commerce_channels_signoff.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/smoke_commerce_channels_signoff.py)
- keep emitting a rollup JSON/Markdown summary for the cohort

### Acceptance Criteria
- current environment gate:
  - every enabled merchant in the cohort has:
    - `catalog_read_ok = true`
    - `catalog_write_ok = true`
    - `payment_order_ok = true`
  - the enabled subset satisfies the cohort's current minimum gate
- long-term target:
  - the enabled subset reaches the cohort's `target_enabled_cases`
  - the enabled subset covers the cohort's `target_semantic_classes`
- every merchant in the cohort that actually runs has:
  - `catalog_read_ok = true`
  - `catalog_write_ok = true`
  - `payment_order_ok = true`
- no merchant has `missing_product_keys_count > 0` after backfill verify
- no merchant requires manual secret cleanup from evidence artifacts
- failures, if any, are attributable to merchant-specific readiness gaps and not to shared platform regressions

## Phase B: Real Payment Completion Path

### Goal
Sign off one real paid terminal-state flow instead of stopping at payment initiation.

### Why This Is A Separate Phase
This phase crosses a different risk boundary:
- real customer-action completion
- real PSP terminal state
- real paid order state convergence

It should not run as part of routine pivot release close-out.

### Scope
- one supervised merchant
- one supervised PSP path
- one low-value transaction amount
- explicit observation of paid-state convergence

### Preconditions
- Phase A is already green, or is explicitly waived
- merchant and PSP are approved for supervised paid testing
- rollback and refund handling are pre-agreed
- operator window is scheduled

### Recommended Surfaces
- [smoke_real_payment_completion_signoff.py](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/smoke_real_payment_completion_signoff.py)
- [smoke_readiness_alpha.sh](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/scripts/smoke_readiness_alpha.sh)
- [DEVELOPER_RUNBOOK.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/docs/readiness/2026-03-agent-commerce/DEVELOPER_RUNBOOK.md)
- [REAL_MERCHANT_ALPHA_REPORT.md](/Users/pengchydan/dev/_worktrees/pivota-backend-hyaluronic-aliases-20260325/docs/readiness/2026-03-agent-commerce/REAL_MERCHANT_ALPHA_REPORT.md)

Use the structured Python wrapper for routine operator evidence collection:
- `--mode preflight` for readiness-owned checkout + order-sync only
- `--mode bridge_paid_reference --payment-reference <psp_ref>` when a real successful PSP reference already exists
- `--mode payment_status_sync` when readiness should create the payment intent and then absorb PSP state

### Execution Shape
1. preflight with the existing order-backed safe canary
2. create or attach a readiness-owned payment intent on the supervised merchant path
3. complete one real payment to a paid terminal state
4. run readiness payment status sync or payment bridge
5. verify paid-state convergence on the order/payment records
6. optionally run one refund validation step if that is in scope

### Acceptance Criteria
- PSP reaches a real successful terminal state
- platform order/payment state converges to paid/completed as designed
- the resulting merchant/order records are internally consistent
- if refund validation is included, refund state converges correctly afterward

### Non-Goals
- not part of every deploy
- not part of routine serve canary close-out
- not required for standard pivot release gates

## Exit Rule
Do not collapse these two phases back into the current release close-out.
If either is needed, create a dedicated evidence folder and a dedicated stage review for that phase alone.
