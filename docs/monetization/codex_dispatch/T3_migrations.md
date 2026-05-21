# Codex prompt — T3: Draft all 17 migrations (14 new tables + 3 extensions)

## Context

Project: Pivota — AI commerce enablement platform.
Working dir: `/Users/pengchydan/dev/pivota-backend-receipt-suppress-fix`
Stack: Python (FastAPI), Postgres (Railway), Stripe (already integrated as PSP).
Architecture spec: `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` — implement v1.3 exactly, do not improvise on architecture.
Existing patterns to follow: see db/, services/, routes/, adapters/ — match style of existing files.
Output: code in the existing repo layout. Migrations as SQL files in db/migrations/.
Don't add new dependencies unless absolutely required. Don't rewrite existing code unless explicitly asked.

## Prerequisite inputs — read these first

1. `docs/monetization/T2_db_audit.md` — migration convention, FK/timestamp/JSONB/index patterns, existing table DDLs, and extension points for the 3 tables being extended. Follow this exactly.
2. `docs/monetization/Pivota_Monetization_System_v1.3_Blueprint.docx` Appendix C — canonical schema deltas for v1.3. This is the source of truth for column names, types, and constraints.

## Task

Write all 17 migration SQL files. File numbering continues from the highest existing migration in db/migrations/. Use the convention documented in T2_db_audit.md (naming, UP/DOWN structure, raw SQL style).

## Tables to create (14 new)

1. `stripe_events` — idempotency log for Stripe webhook events
2. `subscription_plans` — plan catalogue (free/starter/growth/scale tiers)
3. `user_subscriptions` — per-merchant subscription state, linked to Stripe subscription
4. `merchant_credits` — current credit balance + auto-topup config per merchant
5. `credit_ledger` — append-only audit log of every credit-balance change
6. `credit_reservations` — pre-commit credit holds (reserve → commit/release/expire lifecycle)
7. `operation_cost_config` — versioned table of operation type → credit cost
8. `gmv_attribution_daily` — daily rollup of gross/refund/net GMV + take amounts by (date, merchant, agent, partner)
9. `channel_partners` — channel partner registry (name, commission_config_json, connect_account_id)
10. `partner_attribution` — maps commerce_attribution_edges to a channel_partner
11. `partner_balance` — current running balance per channel partner
12. `partner_balance_ledger` — append-only audit log of every partner balance change
13. `invoices` — local mirror of Stripe invoices (draft → finalized → paid/failed)
14. `invoice_disputes` — merchant-filed disputes against invoice line items
15. `billing_runs` — one row per monthly billing cycle execution
16. `billing_run_items` — maps every Stripe InvoiceItem to its source (gmv_attribution_daily row)
17. `settlement_snapshots` — immutable snapshots of partner comp computation (append-only)

## Tables to extend (3)

### `merchants`
Add columns:
- `stripe_customer_id` TEXT — Stripe Customer object ID
- `subscription_id` BIGINT REFERENCES user_subscriptions(id)
- `current_tier` TEXT NOT NULL DEFAULT 'free' — CHECK IN ('free','starter','growth','scale')
- `credits_balance` BIGINT NOT NULL DEFAULT 0 — current credit balance (cents equivalent units)
- `current_period_credit_used` BIGINT NOT NULL DEFAULT 0
- `promo_period_until` TIMESTAMPTZ — NULL means standard take rate applies
- `billing_anchor_day` SMALLINT DEFAULT 1 — day-of-month for billing cycle anchor

### `commerce_attribution_edges`
Add columns:
- `channel_partner_id` BIGINT REFERENCES channel_partners(id)
- `take_rate_applied_bp` SMALLINT — basis points applied (500 = promo, 1000 = standard)
- `gross_attributed_gmv_cents` BIGINT
- `refund_amount_cents` BIGINT NOT NULL DEFAULT 0
- `net_attributed_gmv_cents` BIGINT — computed: max(gross - refund, 0)
- `refunded_at` TIMESTAMPTZ
- `protocol_name` TEXT

### `agent_payouts`
Add columns:
- `payee_type` TEXT NOT NULL DEFAULT 'agent' — CHECK IN ('agent','channel_partner')
- `payee_id` BIGINT — generic ID matching payee_type (replaces hard-coded agent_id for channel partner payouts)
- `comp_config_version` INTEGER
- `snapshot_id` BIGINT REFERENCES settlement_snapshots(id)
- `billing_run_id` BIGINT REFERENCES billing_runs(id)
- `subsidy_cap_remaining_cents` BIGINT
- `clawback_amount_cents` BIGINT NOT NULL DEFAULT 0
- Expand `status` CHECK constraint to include: 'pending','uploaded','paid','approved','failed','clawback_pending'

## Migration requirements

For each migration file:

1. **Naming** — follow the convention from T2_db_audit.md exactly (number prefix + descriptive slug).
2. **UP section** — CREATE TABLE or ALTER TABLE with all columns, constraints, and indexes.
3. **DOWN section** — DROP TABLE or ALTER TABLE DROP COLUMN (reversible). Follow convention from T2.
4. **Types** — be exact:
   - Monetary amounts: `BIGINT` (never INT or NUMERIC for cents)
   - IDs: `BIGSERIAL PRIMARY KEY` for new tables (match existing convention from T2 audit)
   - Timestamps: `TIMESTAMPTZ` (match convention from T2)
   - Enums: `TEXT` + `CHECK` constraint (match existing pattern — no Postgres ENUM types)
   - Payloads: `JSONB`
5. **Indexes** — add indexes on:
   - Every FK column
   - Every lookup column called out in v1.3 (e.g., stripe_events.event_id, billing_runs.idempotency_key, merchant_credits.merchant_id)
   - Composite indexes where v1.3 specifies (e.g., gmv_attribution_daily on (date, merchant_id, agent_id, channel_partner_id))
6. **Unique constraints** — required on:
   - `stripe_events.event_id`
   - `billing_runs.idempotency_key`
   - `merchant_credits.merchant_id` (one row per merchant)
   - `partner_balance.channel_partner_id` (one row per partner)
   - `gmv_attribution_daily` on (date, merchant_id, agent_id, channel_partner_id) — for UPSERT idempotency
7. **Check constraints** — on all enum-like TEXT columns (status, archetype, payee_type, tier, event_type etc.)
8. **Append-only tables** — `credit_ledger`, `partner_balance_ledger`, `settlement_snapshots`: add a trigger or comment noting they are append-only (no UPDATE/DELETE; enforce via trigger if the pattern exists in the codebase).
9. **`set_updated_at()` trigger** — apply to tables that have `updated_at`, matching existing pattern from T2.

## Output

Write all 17 migration files to `db/migrations/`. At the end of the session, print a summary table listing:
- File name
- Table name(s) affected
- UP action (CREATE / ALTER)

## Acceptance criteria

- All 17 migration files written (14 new + 3 extensions). No more, no less.
- File naming matches existing convention from T2_db_audit.md exactly.
- Indexes present on every FK and every lookup column.
- Unique constraints on `stripe_events.event_id`, `billing_runs.idempotency_key`, `merchant_credits.merchant_id`, `partner_balance.channel_partner_id`, `gmv_attribution_daily.(date,merchant_id,agent_id,channel_partner_id)`.
- CHECK constraints on all enum-like columns.
- Monetary amounts use BIGINT throughout.
- Timestamps use TIMESTAMPTZ throughout.
- No Postgres ENUM types — TEXT + CHECK only.
- `settlement_snapshots` has no UPDATE path (append-only enforcement).
- Summary table printed at end of session.

## Don't do

- Don't apply the migrations. Pivota CEO reviews and applies via existing migration tooling.
- Don't add tables beyond the 17 listed.
- Don't optimize prematurely — no partitioning, no materialized views in v1.
- Don't modify any existing source files (Python, config, etc.) — SQL migration files only.
- Don't add new Python dependencies.
- Don't change the existing migration files already in db/migrations/.
