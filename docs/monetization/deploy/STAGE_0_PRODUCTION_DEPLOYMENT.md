# Stage 0 — Production Deployment of v1.3 Monetization

> ⚠️ **Production is GCP Cloud Run (`pivota-prod`, `us-west1`) since 2026-08-22. Railway is the
> ROLLBACK.** The `railway ...` commands below have NOT been rewritten — they were left as-is
> rather than translated by guesswork, because the procedures here were never re-verified against
> GCP. Running one changes the platform nobody is served from: the incident continues while the
> dial reads as turned. Translate with
> [operating_on_gcp_production.md](../../runbooks/operating_on_gcp_production.md) before acting, or treat this
> document as a historical record of how the Railway rollout was done.


Linear deployment plan. Every step has an exact command and an exact verification. Steps that modify prod data or money flow are tagged **AUTHORIZATION REQUIRED**.

- **Deployment target**: Railway → `Pivota Infra` → `production` → service `web` (service id `17b7380b-d0e1-4ff5-8975-516c93cbdc93`, project id `9bdca959-cc79-413c-9f23-c8b5396eb5f0`).
- **Commit to ship**: `73d4631` (current `origin/main` HEAD). Includes monetization-v1.3 (PR #581), Step 6 fixes v1.3.1 (PR #586 + #587), and migration 121 alignment (PR #590).
- **Pre-deploy production commit**: `5838210` (deployment `a020a3e9`). Next merge or `railway up` triggers a new deploy automatically.
- **Single-DB tenancy**: production and staging share one Postgres (`postgres-xmr6`); schema migrations 100–120 are already live from staging deploys. Only **migration 121** has not yet run against the shared DB.

## 1. Pre-deployment inventory

### 22 migrations (db/migrations/100..121)

```bash
ls /path/to/repo/db/migrations/ | grep -E "^1[01][0-9]_|^12[0-1]_"
```

Expected 22 files. Verification query (run once before deploy) — confirms 100–120 already applied; 121 not yet:

```sql
SELECT EXISTS(SELECT 1 FROM information_schema.tables
              WHERE table_schema='public' AND table_name='stripe_events') AS has_100,
       EXISTS(SELECT 1 FROM information_schema.tables
              WHERE table_schema='public' AND table_name='settlement_snapshots') AS has_114,
       (SELECT data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='invoices' AND column_name='billing_period_start') AS inv_period_type,
       (SELECT data_type FROM information_schema.columns
        WHERE table_schema='public' AND table_name='billing_runs' AND column_name='period_start') AS run_period_type;
```

Expected: `has_100=t, has_114=t, inv_period_type='date'` (migration 120 applied), `run_period_type='timestamp with time zone'` (migration 121 NOT YET applied).

### Services to ship

- New: `services/metering_service.py`, `services/gmv_aggregation_service.py`, `services/invoice_generation_service.py`, `services/partner_settlement_service.py`
- Modified: `services/psp_payment_finalizer.py` (T9 attribution stamping hook at line ~228)

### Route to ship

- New: `routes/billing_routes.py` (registered in `main.py:195` import / `main.py:942` `app.include_router(billing_router)`)

### CI must pass

```bash
pytest -xvs tests/test_metering_service.py tests/test_gmv_aggregation_service.py \
            tests/test_invoice_generation_service.py tests/test_attribution_stamping.py
```

Expected: `30 passed, 0 failed`.

## 2. Pre-deployment environment variables

All set on Railway `Pivota Infra → production → web` BEFORE deploy. Fetch current values for diff:

```bash
railway variables --json --environment production --service web | jq 'keys[] | select(test("STRIPE|DATABASE"))'
```

| Variable | Source / Expected Prefix | Verification |
|----------|--------------------------|--------------|
| `STRIPE_SECRET_KEY` | Stripe Live secret. **Must start `sk_live_`** for prod-real billing. | `railway variables --json -e production -s web \| jq -r '.STRIPE_SECRET_KEY' \| head -c 8` → `sk_live_` |
| `STRIPE_WEBHOOK_SECRET` | Commerce webhook signing secret (existing, pre-v1.3). | `\| head -c 6` → `whsec_` |
| `STRIPE_BILLING_WEBHOOK_SECRET` | **NEW for v1.3**. Stripe Live signing secret for `/webhooks/stripe/billing`. Register endpoint URL `https://api.pivota.cc/webhooks/stripe/billing` in Stripe Dashboard (Live mode) → copy secret. | `\| head -c 6` → `whsec_` |
| `DATABASE_URL` | Railway private DNS to `postgres-xmr6`. | host should be `*.railway.internal` |
| `STRIPE_PRICE_ID_STARTER` | Live-mode `price_...` for $99/mo starter tier (must be created in Stripe Live first). | `\| head -c 6` → `price_` |
| `STRIPE_PRICE_ID_GROWTH` | Live-mode `price_...` for $299/mo growth tier. | `\| head -c 6` → `price_` |
| `STRIPE_PRICE_ID_SCALE` | Live-mode `price_...` for $999/mo scale tier. | `\| head -c 6` → `price_` |

**AUTHORIZATION REQUIRED** before changing any env var on production. Cause a redeploy on save.

**Connect platform account note**: v1.3 does not require a separate `STRIPE_CONNECT_PLATFORM_ACCOUNT_ID` env var. T7/T8 use platform `STRIPE_SECRET_KEY` for Customer/Invoice/Transfer calls; merchant Connect account IDs live per-row in `merchant_psps.account_id` and `channel_partners.stripe_connect_account_id`.

**Prerequisite — Stripe Live key + Live Prices**: discovered during Stage 0 prep (2026-05-22) that production `STRIPE_SECRET_KEY` is currently `sk_test_*` (not `sk_live_*`). Commerce paths are unaffected because they run through merchant-scoped credentials in `merchant_psps`, not the platform `STRIPE_SECRET_KEY`. But Live Price creation requires a Live key (Stripe API segregates modes completely). Three steps to complete before Stage 1:

1. **AUTHORIZATION REQUIRED.** Stripe Dashboard → Developers → API Keys (Live mode) → copy `sk_live_*` secret. Set on Railway:
   ```bash
   railway variables --environment production --service web --set "STRIPE_SECRET_KEY=sk_live_..."
   ```
   Railway auto-redeploys. Verify:
   ```bash
   railway variables --json -e production -s web | jq -r '.STRIPE_SECRET_KEY' | head -c 8
   # Expected: sk_live_
   ```

2. **AUTHORIZATION REQUIRED.** Create 3 Live-mode Prices on Live-mode Pivota Products via the codex dispatch in `docs/monetization/codex_dispatch/stripe_live_price_rotation.md`. Pre-flight aborts if STRIPE_SECRET_KEY is still `sk_test_*`, so this must follow step 1.

3. Same codex dispatch handles rotating `STRIPE_PRICE_ID_STARTER` / `_GROWTH` / `_SCALE` env vars to the new Live IDs. Triggers a second Railway redeploy.

Test-mode Prices created in Step 5 remain active in Stripe Test for harness re-runs. Staging env vars keep pointing at them.

## 3. Pre-deployment database backup

**AUTHORIZATION REQUIRED.** Single shared DB; back up before any migration runs.

```bash
# Railway-managed automatic backups: confirm via dashboard
# Railway → Pivota Infra → Postgres-xMr6 → Backups tab → confirm most recent snapshot is <24h old.
# If not, trigger manual snapshot via Railway UI.

# Belt-and-suspenders manual logical backup to local file:
DATABASE_PUBLIC_URL=$(railway variables --json -e production -s Postgres-xMr6 | jq -r '.DATABASE_PUBLIC_URL')
TS=$(date -u +%Y%m%dT%H%M%SZ)
pg_dump "$DATABASE_PUBLIC_URL" -Fc -f "/tmp/pivota-prod-prev-stage0-${TS}.dump"
ls -lh "/tmp/pivota-prod-prev-stage0-${TS}.dump"
```

Verify backup integrity:
```bash
pg_restore --list "/tmp/pivota-prod-prev-stage0-${TS}.dump" | wc -l
# Expected: > 200 (many table/index entries)
```

**Retention**: retain the dump file until **Stage 3 → Stage 4 promotion** (one clean billing cycle on the alpha completes successfully). Real money flows in Stage 3; a regression surfacing then may need schema rollback that requires the Stage 0 backup state. That's 4–6 weeks from now; Postgres custom-format dumps compress small. Worth the storage.

## 4. Migration application order

**Important correction discovered during the 2026-05-22 Stage 0 deploy: the startup migration runner is SKIPPED on production.** Production sets `SKIP_HEAVY_STARTUP_INIT=true` (or defaults to it when `RAILWAY_ENVIRONMENT=production`) so the runner at `main.py:1146–1158` short-circuits before reaching the migration loop at `main.py:1488–1497`. Migrations on the shared DB persist only because **staging deploys** (which don't set the skip flag) ran them. New v1.3 migrations require a **manual apply step** on production.

Of the 22 v1.3 migration files (`100`–`121`), migrations `100`–`120` were already on the shared DB via prior staging deploys (verified by the query in §1). **Migration 121 must be applied manually.**

### Manual migration apply (production)

**AUTHORIZATION REQUIRED.** Apply via the public Postgres proxy. Same pattern as the readiness-check codex paths:

```bash
DATABASE_PUBLIC_URL=$(railway variables --json -e production -s Postgres-xMr6 | jq -r '.DATABASE_PUBLIC_URL')
psql "$DATABASE_PUBLIC_URL" -v ON_ERROR_STOP=on -f db/migrations/121_billing_runs_period_to_date.sql
```

Or for a single-statement migration like 121, inline:
```sql
ALTER TABLE billing_runs
  ALTER COLUMN period_start TYPE DATE USING period_start::date,
  ALTER COLUMN period_end   TYPE DATE USING period_end::date;
```

Verify post-apply:
```sql
SELECT data_type FROM information_schema.columns
WHERE table_schema='public' AND table_name='billing_runs'
  AND column_name IN ('period_start','period_end');
-- Both rows must show data_type='date'.
```

Expected runtime: < 2 seconds (~30 rows in `billing_runs` from prior harness runs).

### Pattern for future migrations

Any new migration added in v1.3.x or v1.4+ deploys to production via the same manual psql path. Future option: add a per-migration admin route following the `routes/admin_run_migration_*` pattern that already exists for migrations 081–099 (callable via authenticated HTTP). Either path works; the startup runner does NOT.

### Reference — all 22 v1.3 migrations

Full enumeration (file + table + columns + notes) is in **Appendix A**. Notable for ops awareness:

- `109` rewrote `commerce_attribution_edges` to add the STORED generated column — applied during a low-traffic window in staging; the shared-DB state means production has already paid this cost.
- `119` ("expanded-scope migration") — briefing refers to this as "migration 085"; renumbered during Wave-2 dedup. It codified T7's runtime `_SCHEMA_GUARD_STATEMENTS` (see `services/invoice_generation_service.py:176–200`), which become defensive no-ops post-119. No further careful handling needed in prod because the shared DB has already run it.

## 5. Code deployment sequence

The Railway service auto-deploys on push to `origin/main`. The commit to ship is `73d4631` (already on main).

```bash
# Verify HEAD that will deploy
git ls-remote origin main | awk '{print $1}'
# Expected: 73d46317c1edc433f055a0796590d809c468a120 (or later if more PRs landed)
```

Trigger the deploy. Two paths — pick one:

**Path A (preferred — already-merged main triggers deploy):** If `73d4631` (or a later commit on main) has not auto-deployed yet, force a redeploy:
```bash
railway redeploy --service web --environment production --yes
```

**Path B (manual upload from local repo):** Only if Path A unavailable.
```bash
cd /path/to/pivota-backend-receipt-suppress-fix
git checkout main && git pull --ff-only
railway up --detach --service web --environment production
```

**AUTHORIZATION REQUIRED** for either path — it's a production code change.

Watch the build:
```bash
railway logs --build --service web --environment production
# Expect: railpack-v0.23.0 detection, pip install, container build, deploy.
```

Verify the new commit is live (~3–6 min after kick):
```bash
curl -sS https://api.pivota.cc/health | jq '{commit:.build.commit_sha[0:7], db_ok, deployment:.build.deployment_id}'
# Expected: commit "73d4631", db_ok true, deployment_id is new.
```

Verify migration 121 applied:
```bash
PROD_URL=$(railway variables --json -e production -s Postgres-xMr6 | jq -r '.DATABASE_PUBLIC_URL')
psql "$PROD_URL" -c "SELECT data_type FROM information_schema.columns WHERE table_schema='public' AND table_name='billing_runs' AND column_name IN ('period_start','period_end');"
# Expected: both rows show data_type=date.
```

## 6. Post-deployment smoke tests

Read-only. None mutate prod data. Each must pass before declaring Stage 0 complete.

### 6.1 New tables present and empty-or-near-empty

```sql
SELECT table_name, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname='public' AND table_name IN (
  'stripe_events','subscription_plans','user_subscriptions','merchant_credits',
  'credit_ledger','credit_reservations','operation_cost_config','channel_partners',
  'gmv_attribution_daily','partner_attribution','partner_balance','partner_balance_ledger',
  'invoices','invoice_disputes','billing_runs','billing_run_items','settlement_snapshots'
)
ORDER BY table_name;
```

Expected: 17 rows. Most counts will be small (harness + Step 6 test data). `subscription_plans` should have 3 rows (starter/growth/scale).

### 6.2 Billing routes registered

```bash
# Should return 405 (route exists; GET not allowed; POST required).
curl -sS -o /dev/null -w "%{http_code}\n" https://api.pivota.cc/webhooks/stripe/billing

# Should return 401 (route exists; auth required).
curl -sS -o /dev/null -w "%{http_code}\n" -X POST https://api.pivota.cc/api/billing/checkout-session

# Both expected. 404 from either = deploy didn't pick up routes/billing_routes.py.
```

### 6.2.1 v1.3 cron jobs registered

```bash
curl -sS https://api.pivota.cc/__scheduler_health | jq '.'
```

Expected: `running: true`, `job_count: 12` (or more), with v1.3 jobs:
- `metering_expire_reservations` — `next_run_time` populated (ACTIVE)
- `gmv_aggregation_daily` — `next_run_time` populated (ACTIVE; next 02:00 UTC)
- `invoice_generation_monthly` — `next_run_time: null` (PAUSED)
- `partner_settlement_monthly` — `next_run_time: null` (PAUSED)

If the v1.3 jobs are missing, PR #592 didn't take — investigate scheduler init logs.

### 6.3 T9 attribution stamping wired

```bash
# Service imports successfully (no syntax / import errors). Confirms psp_payment_finalizer landed cleanly.
railway ssh --project 9bdca959-cc79-413c-9f23-c8b5396eb5f0 --environment production --service web \
  "python3 -c 'from services.psp_payment_finalizer import stamp_gross_attributed_gmv; print(\"ok\")'"
# Expected stdout: ok
```

### 6.4 Existing commerce webhook unaffected

```sql
SELECT MAX(received_at) FROM webhook_events WHERE provider='stripe';
-- Expected: recent timestamp from real commerce webhook traffic. Confirms /webhooks/stripe still serves commerce.
```

## 7. Rollback procedure

Decision criteria for rollback are in §9. If invoked, run in this order.

### 7.1 Code rollback (no data risk)

```bash
# Revert to pre-v1.3 production deploy.
railway redeploy --service web --environment production --deployment-id <prev_success_id> --yes
# Find prev_success_id via: railway deployment list --service web --environment production
```

Verify:
```bash
curl -sS https://api.pivota.cc/health | jq -r '.build.commit_sha[0:7]'
# Expected: the prev commit (not 73d4631).
```

### 7.2 Schema rollback (only if data corruption suspected)

**AUTHORIZATION REQUIRED.** All v1.3 migrations are forward-only (DOWN sections are commented in the migration files for reference — they are NOT run by the startup runner).

For migration 121 specifically — only schema change applied on this deploy — the rollback is:
```sql
-- AUTHORIZATION REQUIRED
ALTER TABLE billing_runs
  ALTER COLUMN period_start TYPE TIMESTAMPTZ USING period_start::timestamptz,
  ALTER COLUMN period_end   TYPE TIMESTAMPTZ USING period_end::timestamptz;
```

For migrations 100–120 — full rollback requires DROP of 17 tables and ALTER DROP COLUMN on 3 (merchants, commerce_attribution_edges, agent_payouts). Manual sequence in each migration's `-- DOWN` comment block. **Data loss: all subscription/billing/settlement state is destroyed.** Restore from §3 pg_dump if going this route.

### 7.3 Env var rollback

`AUTHORIZATION REQUIRED.` Per variable changed in §2:
```bash
railway variables --set STRIPE_BILLING_WEBHOOK_SECRET="<previous_value_or_unset>" -e production -s web
```

Unsetting `STRIPE_BILLING_WEBHOOK_SECRET` causes `/webhooks/stripe/billing` to return 400 on every call — protects against accidental write. No data is touched.

## 8. Cron schedule registration table

**Current state**: zero v1.3 cron jobs are registered in `services/audit_scheduler.py` or `jobs/`. Services are imports-only on this deploy. T9 stamping fires inline on every `finalize_payment_success` call — no cron needed.

| Job | Schedule (planned) | Stage enabled | Code location to add |
|-----|--------------------|---------------|---------------------|
| T9 attribution stamping | inline; no cron | Stage 0 (auto on deploy) | already wired in `services/psp_payment_finalizer.py:228` |
| T6 GMV aggregation | daily 02:00 UTC | Stage 1 | not yet written — Stage 1 prerequisite, see Stage 1 §0 |
| T7 invoice generation | monthly 03:00 UTC on day 2 | Stage 4+ | not yet written |
| T8 partner settlement | monthly 04:00 UTC on day 3 (after T7) | Stage 4+ | not yet written |
| T5 metering reaper (`expire_stale_reservations`) | every 5 minutes | Stage 1 (once T5 is called by anything) | not yet written |

**Stage 0 toggle for crons**: not applicable — none registered. **Stage 1 prerequisite**: register T6 + T5 crons. This is a code change outside this documentation round; flagged in §9 and in `questions_for_cowork.md`.

## 9. On-call

- **On point during deploy**: Jack (Cowork) — architecture decisions. Claude Code — orchestration / SQL verification / log triage.
- **Expected duration**: 15 minutes for code deploy + migration 121 + smoke tests. Add 30 minutes buffer for env var rotation if `STRIPE_PRICE_ID_*` go Live.
- **Rollback decision criteria**:
  - Health endpoint reports `db_ok=false` for >2 min after deploy → roll back.
  - Migration 121 fails to apply (verification in §5 shows `period_start` still `timestamp with time zone`) → roll back code, investigate.
  - Existing `/webhooks/stripe` returns non-200 for any live commerce webhook (per `webhook_events.last_status_code`) → roll back immediately; commerce takes priority over monetization.
  - Smoke test 6.2 returns 404 → routes/billing_routes.py didn't register → roll back, check main.py:942 in deployed source.
- **Stage 0 unknowns to resolve before execution** (none are blockers, but call out):
  - **Stripe Live Prices**: `STRIPE_PRICE_ID_*` env vars are currently set to Test-mode IDs. Stage 3+ requires Live equivalents. Decide whether to rotate now (Stage 0) or defer.
  - **T6 cron + T5 reaper**: must be registered before Stage 1 can validate GMV rollups or claim "shadow mode running". This is a code change; see Stage 1 §0.

---

## Appendix A — Full migration enumeration

All 22 v1.3 monetization migrations, in apply order. Migrations 100–120 already applied to the shared DB from staging deploys; 121 runs on this deploy.

| File | Adds | Notes |
|------|------|-------|
| 100_stripe_events.sql | `stripe_events` (event_id UNIQUE for webhook idempotency) | — |
| 101_subscription_plans.sql | `subscription_plans` (4-tier catalog) | — |
| 102_user_subscriptions.sql | `user_subscriptions` (one row per Stripe Subscription) | FK to subscription_plans |
| 103_extend_merchants_monetization.sql | `merchants` + 7 cols (stripe_customer_id, subscription_id, current_tier, credits_balance, current_period_credit_used, promo_period_until, billing_anchor_day) | adds FK to user_subscriptions |
| 104_merchant_credits.sql | `merchant_credits` (T5 SELECT FOR UPDATE target) | UNIQUE merchant_id |
| 105_credit_reservations.sql | `credit_reservations` (status: reserved → committed/released/expired) | — |
| 106_credit_ledger.sql | `credit_ledger` (append-only audit) | — |
| 107_operation_cost_config.sql | versioned operation→cost table | — |
| 108_channel_partners.sql | partner registry | — |
| 109_extend_commerce_attribution_edges_monetization.sql | adds gross/refund/net cents + generated column | `net_attributed_gmv_cents BIGINT GENERATED ALWAYS AS STORED` — required table rewrite in staging |
| 110_gmv_attribution_daily.sql | daily rollup table | unique expression index: `(date, merchant_id, COALESCE(agent_id, ''), COALESCE(channel_partner_id, -1))` |
| 111_partner_attribution.sql | edge→partner mapping | — |
| 112_partner_balance.sql | partner running balance | UNIQUE channel_partner_id |
| 113_billing_core.sql | `billing_runs`, `invoices`, `invoice_disputes`, `billing_run_items` (4 tables) | unique idempotency_key on billing_runs |
| 114_settlement_snapshots.sql | immutable snapshot table | append-only trigger |
| 115_partner_balance_ledger.sql | partner balance audit log | append-only |
| 116_extend_agent_payouts_monetization.sql | extends agent_payouts (payee_type, payee_id, billing_run_id, snapshot_id, subsidy_cap_remaining_cents, clawback_amount_cents) | expands status CHECK |
| 117_metering_service_columns.sql | adds metadata to credit_reservations, source_type to credit_ledger | — |
| 118_invoice_payment_failed_status.sql | adds invoices.paid_at, expands ck_invoices_status | — |
| 119_invoice_finalizing_status.sql | **expanded-scope ("085")** — adds `'finalizing'` to ck_invoices_status, `'applied'` to ck_invoice_disputes_status, plus columns billing_run_id/stripe_customer_id on invoices, voided_at on billing_run_items, disputed_line_items_jsonb on invoice_disputes | Codifies T7's runtime `_SCHEMA_GUARD_STATEMENTS` |
| 120_invoices_billing_period_to_date.sql | TIMESTAMPTZ → DATE on invoices.billing_period_start/end | requires brief table lock; ran during low-traffic |
| 121_billing_runs_period_to_date.sql | TIMESTAMPTZ → DATE on billing_runs.period_start/end | **applies on this deploy** |
