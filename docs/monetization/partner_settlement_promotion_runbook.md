# Partner Settlement Promotion Runbook (Blocker #3)

**Goal.** Turn on real channel-partner rev-share: flip `PARTNER_REV_SHARE_USE_V2`
and resume the two paused settlement crons (T7 invoice generation, T8 partner
settlement), so attributed brands actually pay their partner.

**Status when written (2026-07-07).** Engine + pipeline are built and unit-tested
but have **never run against real data**. T7/T8 are registered *paused*
(`next_run_time=None`) in `services/audit_scheduler.py`. `PARTNER_REV_SHARE_USE_V2`
defaults to `false`. This runbook is the safe path to flip them on.

> ⚠️ Several steps are **money-generating and outward-facing** (merchant invoices,
> then Stripe Connect transfers to partners). They are hard to reverse. Do not
> automate them end-to-end; run each step, verify, then proceed.

---

## The pipeline, and why order matters

```
T7 invoice_generation_monthly (day 2)  → freezes monthly_brand_statements + billing_runs row
        │                                 (v2 engine only reads status IN frozen/invoiced)
        ▼
T8 partner_settlement_monthly (day 3)  → compute_partner_comp_v2 → write settlement_snapshots
        │                                 (INTERNAL rows; no external money)
        ▼
settlement_file_generate (day 5, ACTIVE) → aggregate snapshots → settlement_files (DB only)
        │
        ▼
settlement_file_transfer (day 10, ACTIVE) → Stripe Connect transfers  ← REAL MONEY OUT
```

Two hard invariants:

1. **`PARTNER_REV_SHARE_USE_V2=true` must be set before T8 runs.** With the flag
   off, `run_settlement()` (a) computes from the **deprecated**
   `commission_config_json` (empty for new partners → wrong/zero comp) and
   (b) also runs the legacy `partner_balance` + `agent_payouts` payout path.
   Since the day-5/day-10 file pipeline *also* pays from the same snapshots,
   running with the flag off risks **double-paying**. The flag being on makes
   `run_settlement` write the snapshot and then `continue` (skip legacy payout).
2. **T7 must complete before T8**, or there are no frozen statements to settle.

---

## Step 0 — Dry run (READ ONLY, do this first)

Verifies the v2 engine computes sane numbers against real data. Writes nothing.

```bash
railway run --service web python scripts/partner_settlement_dry_run.py
# specific period / billing run:
railway run --service web python scripts/partner_settlement_dry_run.py --billing-run-id <id>
railway run --service web python scripts/partner_settlement_dry_run.py --json
```

Pass criteria:
- It finds the latest completed `billing_runs` row (or use `--period-*`).
- Per-partner `net`, `sub`, `ovg`, `gmv` look plausible; `active_rate_scope` /
  `gmv_take_definition` match the contract.
- Brands you expect to pay show under `brands ✓`; brands not yet activated show
  under `⊘act` (that's the lifecycle gate from PR #1192 — expected for
  brand-new referrals with no paid invoice yet).
- `GRAND TOTAL` is in the range you expect.

If a partner you expect to pay shows `⊘act` for every brand, their merchants
never hit the activation event (no paid positive-net invoice) — investigate
before promoting; it is not a settlement bug.

## Step 1 — Flip the flag (reversible, inert while crons paused)

Set on the Railway **web** service:

```
PARTNER_REV_SHARE_USE_V2=true
```

This changes nothing until a settlement actually runs (both crons are still
paused), so it is safe to set now.

**Verifying it took (corrected 2026-08-11).** No HTTP probe publishes this flag —
not `/version`, not `/config-check`, not `/admin/config/check`. An earlier
revision of this runbook said to "verify via `/version`", and a note I added on
top of it implied an admin token would reveal the value; both were wrong.
`/version` only tells you **which build is live**, which is the part that matters
here: the flag is read at import time in `config/settings.py`, so a Railway
variable change needs a redeploy to take effect.

So verify in two steps:

```bash
railway variables --service web --json | grep PARTNER_REV_SHARE_USE_V2
```

then confirm the running build is newer than the variable change:

```bash
curl -s https://api.pivota.cc/version
```

(`/version`'s `settings_contract` block became ADMIN-ONLY on 2026-08-11 — it was
publishing the rate-limit threshold and whether discount reconciliation is
enforcing to anonymous callers. The `version` / `commit_time` fields you need
here are still public.)

Rollback: set back to `false` **and redeploy** — the variable alone is inert.

## Step 2 — Resume T7, then T8

Use the **guarded admin endpoint** (admin auth, allowlisted to the settlement
job ids, no deploy, one-call rollback):

```bash
# inspect current state of the four managed jobs
curl -sS -H "Authorization: Bearer $ADMIN_JWT" https://api.pivota.cc/admin/scheduler/jobs
# resume T7 first, then T8
curl -sS -X POST -H "Authorization: Bearer $ADMIN_JWT" \
  https://api.pivota.cc/admin/scheduler/jobs/invoice_generation_monthly/resume
curl -sS -X POST -H "Authorization: Bearer $ADMIN_JWT" \
  https://api.pivota.cc/admin/scheduler/jobs/partner_settlement_monthly/resume
```

Each call returns `{action, id, paused, next_run_time, trigger}`. `resume` is a
no-op if already running; `pause` (rollback) a no-op if already paused. Only the
four settlement job ids are accepted — any other id returns 403.

Fallback mechanisms if the endpoint is unavailable: a runtime
`get_scheduler().resume_job(<id>)` in a service shell, or removing
`next_run_time=None` from the two `_add_job` calls in
`services/audit_scheduler.py` (lines ~523 and ~540) and redeploying (least
controllable — avoid for the first run).

Resume **T7 first**. On day 2 it freezes the prior month's statements and writes
a `completed` billing_runs row. Confirm via `scheduler_health` (job shows a
`next_run_time`) and that a fresh `billing_runs` row + frozen
`monthly_brand_statements` appear. **Merchant invoices are generated here — this
is the first outward-facing step.**

Then resume **T8**. It writes `settlement_snapshots` only (internal). Re-run the
dry-run's numbers against the actual snapshot rows to confirm parity.

## Step 3 — Verify before day 10 (the point of no return)

`settlement_file_generate` (day 5) turns snapshots into `settlement_files`
(DB only). **Inspect every `settlement_files` row before day 10.** Check
`transfer_amount_cents`, `carryover_*`, and that each paying partner has a valid
`stripe_connect_account_id` (else the transfer fails with
`no_stripe_connect_account`). `settlement_file_transfer` (day 10) then issues
**real Stripe Connect transfers** — after this, money has moved.

If a transfer fails, the staff-only retry endpoint is
`POST /admin/partners/{id}/settlements/{file_id}/retry`.

---

## Rollback

| Undo | How |
|---|---|
| Flag | `PARTNER_REV_SHARE_USE_V2=false` |
| Crons | `POST /admin/scheduler/jobs/{id}/pause` (one call, no deploy) |
| Snapshots | immutable by design — do **not** hand-edit; a bad run is corrected by clawback/adjustment ledger entries, not deletion |
| Transfers | cannot be un-sent; reconcile via Stripe refund/adjustment out of band |

## DB isolation caveat

Historically staging shared a single Postgres with production
(`project_pivota_infra_single_db`, 2026-05-23). **Confirm current isolation
before treating any "staging" run as safe** — if the DB is shared, a "staging"
settlement run writes real snapshots. The Step 0 dry-run is safe regardless
because it is SELECT-only.

## Follow-up worth building

- A **contract-terms editor** and **cohort-target creation** endpoint (partner
  detail is otherwise read-only after creation).
- Wiring all of these endpoints into the employee-portal UI (they exist as APIs
  only today).
