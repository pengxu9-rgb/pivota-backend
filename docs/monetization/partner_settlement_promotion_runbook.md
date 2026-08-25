# Partner Settlement Promotion Runbook (Blocker #3)

> **Production is GCP Cloud Run (`pivota-prod`, `us-west1`), not Railway.** Commands below were
> rewritten for it on 2026-08-25. This runbook's own invariant warns that a missed flag risks
> DOUBLE-PAYING — reading the flag off the rollback is exactly how that happens. See
> [operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md).

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

There is no `railway run` equivalent on Cloud Run — no shell, no host to attach to. Use a
throwaway job on the production image. The full pattern and its three footguns are in
[operating_on_gcp_production.md](../runbooks/operating_on_gcp_production.md); the short form:

```bash
JOB="settlement-dryrun-$$-$RANDOM"
gcloud run jobs create "$JOB" --project pivota-prod --region us-west1 \
  --image us-west1-docker.pkg.dev/pivota-shared/pivota/backend:latest \
  --service-account sa-worker@pivota-prod.iam.gserviceaccount.com \
  --network default --subnet default --vpc-egress all-traffic \
  --set-secrets DATABASE_URL=DATABASE_URL:latest \
  --max-retries 0 --task-timeout 600s \
  --command python --args=scripts/partner_settlement_dry_run.py
gcloud run jobs execute "$JOB" --project pivota-prod --region us-west1 --wait || {
  echo "job FAILED - do not read the log for a verdict, the exit code already gave you one" >&2; }
for i in 1 2 3 4 5 6; do
  OUT=$(gcloud logging read "resource.labels.job_name=\"$JOB\"" --project pivota-prod \
    --limit 200 --format='value(textPayload)' --freshness=10m)
  [ -n "$OUT" ] && break
  sleep 5
done
printf '%s\n' "$OUT"
gcloud run jobs delete "$JOB" --project pivota-prod --region us-west1 --quiet
```

For a specific period or billing run, extend `--args` — note gcloud splits it on COMMAS, so use the
alternate-delimiter form when passing more than one:

```bash
  --args="^|^scripts/partner_settlement_dry_run.py|--billing-run-id|<id>"
  --args="^|^scripts/partner_settlement_dry_run.py|--json"
```

**Secrets are not inherited by a job.** Without the `--set-secrets` line the script reads no
`DATABASE_URL` and fails in a way that looks like a database outage rather than a missing mount.

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

Set on the Cloud Run **web** service:

```bash
gcloud run services update web --project pivota-prod --region us-west1 \
  --update-env-vars PARTNER_REV_SHARE_USE_V2=true
```

`--update-env-vars` MERGES. `--set-env-vars` and `--env-vars-file` remove every existing plain
env var first — on `web` that is 192 of them — so reaching for the wrong one here replaces a
reversible flag flip with a configuration outage.

This changes nothing until a settlement actually runs (both crons are still
paused), so it is safe to set now.

**Check first — it may already be set.** As of 2026-08-25 `PARTNER_REV_SHARE_USE_V2` is already
`true` on prod `web`. Run the verify command below before the update; flipping a flag that is
already flipped rolls a pointless revision.

**Verifying it took (corrected 2026-08-11).** No HTTP probe publishes this flag —
not `/version`, not `/config-check`, not `/admin/config/check`. An earlier
revision of this runbook said to "verify via `/version`", and a note I added on
top of it implied an admin token would reveal the value; both were wrong.
`/version` only tells you **which build is live**, which is the part that matters
here: the flag is read at import time in `config/settings.py`, so changing the
variable needs new processes to take effect.

So verify in two steps:

```bash
gcloud run services describe web --project pivota-prod --region us-west1 --format=json \
  | python3 -c 'import json,sys; e=json.load(sys.stdin)["spec"]["template"]["spec"]["containers"][0].get("env",[]); print([x for x in e if x["name"]=="PARTNER_REV_SHARE_USE_V2"] or "ABSENT")'
```

then confirm NEW PROCESSES picked it up, by checking the serving revision changed:

```bash
gcloud run services describe web --project pivota-prod --region us-west1 \
  --format='value(status.latestReadyRevisionName)'
```

**Not `/version`.** `services update --update-env-vars` reuses the same image, so the build SHA is
unchanged by construction — curling `/version` before and after an env-only change returns the
identical value whether or not it applied. The revision name is what moves. (`/version`'s
`settings_contract` block became ADMIN-ONLY on 2026-08-11; it was publishing the rate-limit
threshold and whether discount reconciliation is enforcing, to anonymous callers.)

Rollback: `--update-env-vars PARTNER_REV_SHARE_USE_V2=false`. On Cloud Run that IS the redeploy —
`services update` rolls a new revision and shifts traffic to it once healthy, so unlike Railway
there is no separate step and no window where the variable is set but inert.

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
