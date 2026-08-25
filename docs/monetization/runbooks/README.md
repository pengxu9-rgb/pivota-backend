# Pivota Monetization v1.3 Runbooks

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


Operational playbooks for Pivota's monetization stack. Surfaced by the Week 9 Test Clock dry run on 2026-05-21. Format: **Symptom → Investigation → Resolution → Prevention**.

| # | Runbook | Trigger |
|---|---------|---------|
| 01 | [Stripe webhook signature mismatch](01_stripe_webhook_signature_mismatch.md) | `/webhooks/stripe/billing` returns 400; events stop flowing |
| 02 | [Duplicate event_id collision](02_duplicate_event_id.md) | Stripe redelivers an event; `stripe_events` already has it |
| 03 | [Finalize on already-finalized invoice](03_finalize_on_finalized_invoice.md) | `invoices.status='finalizing'` stuck; Stripe says invoice is finalized |
| 04 | [Missing connect_account on payout](04_missing_connect_account.md) | `execute_payout` raises `PayoutMissingConnectAccountError` |
| 05 | [Stripe Transfer failure mid-settlement](05_stripe_transfer_failure.md) | `agent_payouts.status='failed'`; partner balance still positive |
| 06 | [Partial billing run failure](06_partial_billing_run_failure.md) | A merchant is missing from `invoices` after `run_billing_cycle` completes (v1.4 gap workaround) |
| 07 | [Orphaned credit reservation past expiry](07_credit_reservation_orphan.md) | Merchant credits_balance lower than expected; reservations stuck `reserved` past `expires_at` |
| 08 | [GMV aggregation date-boundary drift](08_gmv_date_boundary_drift.md) | `gmv_attribution_daily` totals don't match raw edges; off-by-one day |
| op-01 | [Brand dispute out of window](op_01_brand_dispute_out_of_window.md) | Merchant disputes a charge after the 30-day reconciliation window |
| op-02 | [Markato payout short](op_02_markato_payout_short.md) | Channel partner reports their payout was lower than expected |

## How to use

- Each runbook fits on one screen.
- "Investigation" SQL is read-only and safe to run via the Railway public Postgres proxy or `railway ssh psql`.
- "Resolution" SQL may include `UPDATE`/`INSERT` — flag at the top of each that requires authorization. Never run `DELETE` or `DROP` from a runbook without explicit per-incident sign-off.
- All write paths should also leave a row in `credit_ledger`, `partner_balance_ledger`, or `settlement_snapshots` so the action is auditable.

## v1.4 followups linked from runbooks

- Runbook 06 documents the operator workaround for the per-merchant retry gap; v1.4 will codify it as `retry_failed_billing_run_items` + admin endpoint (see `questions_for_cowork.md`).
- Runbook op-01 mentions the 90-day soft window admin UI (out of scope for v1.3).
- Runbook op-02 mentions a weekly variance-check cron (out of scope for v1.3).
