-- 125_channel_partner_contract_schema.sql
-- Generic channel-partner contract schema. Hoists contract terms out of
-- channel_partners.commission_config_json into queryable / constrainable
-- columns, introduces per-(partner, scope, stream, brand_year) rate
-- schedules with effective-dating (so config flag flips don't retroactively
-- recompute settled snapshots — build brief §8), and captures cohort
-- accelerator targets (cohort target bonus mechanic, per the 2026-05-24
-- architectural decision; chosen over per-brand Y1 rate bump and one-time
-- bounty alternatives recommended by Track B industry research).
--
-- PR #1 of the channel-partner program rebuild (gap analysis 2026-05-23).
-- Subsequent PRs (#2..#9) consume the new columns; this migration only
-- adds structure + seeds, no behavior changes. The legacy
-- commission_config_json JSONB column is kept and marked deprecated so
-- v1.3 partner_settlement_service.py continues working until the rev-share
-- engine v2 cuts over (PR #5).
--
-- NOTE on naming: this migration never references a specific partner in
-- DDL — "Markato" appears only inside seed-row WHERE clauses keyed off
-- legal_name. Future partners follow the same shape.

-- ---------------------------------------------------------------------------
-- 1) Archetype enum cleanup.
--    Drop the legacy 'markato' literal from the CHECK constraint and add
--    the generic 'curated_marketplace' typology. Existing rows that used
--    'markato' migrate to 'curated_marketplace'.
-- ---------------------------------------------------------------------------

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_archetype;

UPDATE channel_partners
SET archetype = 'curated_marketplace'
WHERE archetype = 'markato';

ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_archetype CHECK (
    archetype IN (
      'curated_marketplace',
      'agency',
      'affiliate',
      'platform',
      'protocol_partner',
      'other'
    )
  );

-- ---------------------------------------------------------------------------
-- 2) Structured contract-term columns.
--    All defaults match build brief defaults so existing rows are valid
--    immediately after the migration.
-- ---------------------------------------------------------------------------

ALTER TABLE channel_partners
  ADD COLUMN IF NOT EXISTS term_start_date DATE,
  ADD COLUMN IF NOT EXISTS term_months INTEGER NOT NULL DEFAULT 12,
  ADD COLUMN IF NOT EXISTS term_auto_renew BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS per_brand_tail_months INTEGER NOT NULL DEFAULT 36,
  ADD COLUMN IF NOT EXISTS churn_clawback_days INTEGER NOT NULL DEFAULT 90,
  ADD COLUMN IF NOT EXISTS nonpayment_clawback_days INTEGER NOT NULL DEFAULT 60,
  ADD COLUMN IF NOT EXISTS per_brand_subsidy_cap_cents BIGINT,
  ADD COLUMN IF NOT EXISTS gmv_take_rate_bp INTEGER NOT NULL DEFAULT 1000,
  ADD COLUMN IF NOT EXISTS active_rate_scope TEXT NOT NULL DEFAULT 'B',
  ADD COLUMN IF NOT EXISTS gmv_take_definition TEXT NOT NULL DEFAULT 'net',
  ADD COLUMN IF NOT EXISTS prepaid_credits_supported BOOLEAN NOT NULL DEFAULT TRUE,
  ADD COLUMN IF NOT EXISTS monthly_overage_supported BOOLEAN NOT NULL DEFAULT TRUE;

-- Constraints are dropped-then-added so re-apply under the AUTOCOMMIT
-- migration runner (main.py:1483) stays idempotent. The runner catches
-- duplicate-object errors as warnings, but the file-level exception
-- handler would abort subsequent statements; explicit DROP IF EXISTS
-- avoids the failure entirely.
ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_term_months_positive;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_term_months_positive
  CHECK (term_months > 0);

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_tail_months_positive;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_tail_months_positive
  CHECK (per_brand_tail_months > 0);

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_clawback_days_positive;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_clawback_days_positive
  CHECK (churn_clawback_days > 0 AND nonpayment_clawback_days > 0);

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_subsidy_cap_nonneg;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_subsidy_cap_nonneg
  CHECK (per_brand_subsidy_cap_cents IS NULL OR per_brand_subsidy_cap_cents >= 0);

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_gmv_take_rate_range;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_gmv_take_rate_range
  CHECK (gmv_take_rate_bp BETWEEN 0 AND 10000);

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_active_rate_scope;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_active_rate_scope
  CHECK (active_rate_scope IN ('A', 'B', 'C'));

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_gmv_take_definition;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_gmv_take_definition
  CHECK (gmv_take_definition IN ('gross', 'net', 'channel_tiered'));

ALTER TABLE channel_partners
  DROP CONSTRAINT IF EXISTS ck_channel_partners_billing_mode_supported;
ALTER TABLE channel_partners
  ADD CONSTRAINT ck_channel_partners_billing_mode_supported
  CHECK (prepaid_credits_supported OR monthly_overage_supported);

COMMENT ON COLUMN channel_partners.per_brand_tail_months IS
  'Per-brand rev-share tail in months from brand activation (default 36). Per-brand anchoring (not per-partner term) per 2026-05-23 architectural decision; partner term acts only as a submission window for new brands.';
COMMENT ON COLUMN channel_partners.churn_clawback_days IS
  'Window in days from brand activation in which churn reverses all accrued shares (build brief §7.2, default 90).';
COMMENT ON COLUMN channel_partners.nonpayment_clawback_days IS
  'Brand-invoice age in days that suspends partner accrual until settled (build brief §7.3, default 60).';
COMMENT ON COLUMN channel_partners.per_brand_subsidy_cap_cents IS
  'Onboarding subsidy cap PER BRAND (build brief §7.4 default $5,000). NULL = no cap. Cents.';
COMMENT ON COLUMN channel_partners.gmv_take_rate_bp IS
  'Pivota gmv-take rate as basis points (default 1000 = 10%). Per-partner from day 1 per 2026-05-23 decision.';
COMMENT ON COLUMN channel_partners.active_rate_scope IS
  'Active rate-schedule scope (A/B/C). Resolves to a row in partner_rate_schedules.';
COMMENT ON COLUMN channel_partners.gmv_take_definition IS
  'How partner gmv-share is computed: gross | net | channel_tiered (build brief §6.5). Default net.';
COMMENT ON COLUMN channel_partners.commission_config_json IS
  'DEPRECATED — read by v1.3 partner_settlement_service.py; replaced by structured contract columns + partner_rate_schedules. Cut over in rev-share engine v2 (PR #5 of the channel-partner program rebuild).';

-- ---------------------------------------------------------------------------
-- 3) partner_rate_schedules — per (partner × scope × stream × brand_year),
--    effective-dated.
--    Build brief §6.4 default rates (Scope B) are 27/17/30 (Y1),
--    17/12/22 (Y2), 7/7/12 (Y3) for subscription / credit_overage /
--    gmv_take respectively. Beyond brand_year 3 the partner earns 0;
--    no row needed (resolver returns 0 on miss).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS partner_rate_schedules (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  scope TEXT NOT NULL,
  stream TEXT NOT NULL,
  brand_year SMALLINT NOT NULL,
  rate_bp INTEGER NOT NULL,
  effective_from DATE NOT NULL,
  effective_to DATE,
  notes TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_partner_rate_schedules_scope
    CHECK (scope IN ('A', 'B', 'C')),
  CONSTRAINT ck_partner_rate_schedules_stream
    CHECK (stream IN (
      'subscription',
      'credit_overage',
      'gmv_take',
      'gmv_take_personal',
      'gmv_take_third_party'
    )),
  CONSTRAINT ck_partner_rate_schedules_brand_year
    CHECK (brand_year BETWEEN 1 AND 3),
  CONSTRAINT ck_partner_rate_schedules_rate_range
    CHECK (rate_bp BETWEEN 0 AND 10000),
  CONSTRAINT ck_partner_rate_schedules_effective_window
    CHECK (effective_to IS NULL OR effective_to > effective_from)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_partner_rate_schedules_lookup
  ON partner_rate_schedules (channel_partner_id, scope, stream, brand_year, effective_from);

CREATE INDEX IF NOT EXISTS idx_partner_rate_schedules_partner_scope
  ON partner_rate_schedules (channel_partner_id, scope);

COMMENT ON TABLE partner_rate_schedules IS
  'Per-partner rev-share rates keyed by (scope, stream, brand_year). Effective-dated; flag changes never retroactively recompute settled snapshots (build brief §8).';
COMMENT ON COLUMN partner_rate_schedules.stream IS
  'Revenue stream this rate applies to. gmv_take is used when gmv_take_definition in (gross, net); gmv_take_personal / gmv_take_third_party are used when gmv_take_definition = channel_tiered (build brief §6.5).';

-- ---------------------------------------------------------------------------
-- 4) partner_cohort_targets — accelerator clauses.
--    Encodes the cohort-target-bonus accelerator chosen 2026-05-24.
--    Schema supports >1 tiered target per partner so future contracts can
--    layer (e.g., 10 brands → $X, 20 brands → $Y).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS partner_cohort_targets (
  id BIGSERIAL PRIMARY KEY,
  channel_partner_id BIGINT NOT NULL REFERENCES channel_partners(id) ON DELETE RESTRICT,
  label TEXT NOT NULL,
  target_brand_count INTEGER NOT NULL,
  window_months INTEGER NOT NULL,
  window_start_date DATE NOT NULL,
  bonus_cents BIGINT NOT NULL,
  status TEXT NOT NULL DEFAULT 'open',
  achieved_at TIMESTAMPTZ,
  paid_at TIMESTAMPTZ,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  CONSTRAINT ck_partner_cohort_targets_label_nonempty CHECK (label <> ''),
  CONSTRAINT ck_partner_cohort_targets_count_positive CHECK (target_brand_count > 0),
  CONSTRAINT ck_partner_cohort_targets_window_positive CHECK (window_months > 0),
  CONSTRAINT ck_partner_cohort_targets_bonus_nonneg CHECK (bonus_cents >= 0),
  CONSTRAINT ck_partner_cohort_targets_status
    CHECK (status IN ('open', 'achieved', 'paid', 'expired'))
);

CREATE INDEX IF NOT EXISTS idx_partner_cohort_targets_partner
  ON partner_cohort_targets (channel_partner_id, status);

-- set_updated_at() is defined by migration 108_channel_partners.sql and is
-- already present in any DB that has channel_partners.
DROP TRIGGER IF EXISTS trg_partner_cohort_targets_updated_at ON partner_cohort_targets;
CREATE TRIGGER trg_partner_cohort_targets_updated_at
  BEFORE UPDATE ON partner_cohort_targets
  FOR EACH ROW EXECUTE PROCEDURE set_updated_at();

COMMENT ON TABLE partner_cohort_targets IS
  'Cohort accelerator clauses (e.g., "first N brands in M months → bonus"). Supports multiple tiered targets per partner.';

-- ---------------------------------------------------------------------------
-- 5) Seed.
--    Backfill the structured contract columns for any existing
--    curated_marketplace rows whose legal_name starts with "Markato".
--    Brief defaults: term_months 12, per_brand_tail_months 36,
--    churn 90d, nonpayment 60d, per-brand subsidy cap $5,000,
--    gmv_take 10%, scope B, gmv_take_definition 'net'. Cohort target
--    20 brands within the partner term (bonus dollar amount is open in
--    term-sheet negotiation; seeded at 0 to record structure, set later).
-- ---------------------------------------------------------------------------

UPDATE channel_partners
SET
  term_start_date = CURRENT_DATE,
  term_months = 12,
  term_auto_renew = TRUE,
  per_brand_tail_months = 36,
  churn_clawback_days = 90,
  nonpayment_clawback_days = 60,
  per_brand_subsidy_cap_cents = 500000,
  gmv_take_rate_bp = 1000,
  active_rate_scope = 'B',
  gmv_take_definition = 'net'
WHERE archetype = 'curated_marketplace'
  AND legal_name ILIKE 'markato%'
  AND term_start_date IS NULL;  -- gate seed to first apply only; preserves
                                 -- any manual tuning on re-apply (staging
                                 -- migration runner re-fires on every deploy)

-- Seed Scope B rates for Markato (build brief §6.4). Idempotent via the
-- unique (channel_partner_id, scope, stream, brand_year, effective_from)
-- index — re-apply with the same effective_from is a no-op.
INSERT INTO partner_rate_schedules
  (channel_partner_id, scope, stream, brand_year, rate_bp, effective_from, notes)
SELECT
  cp.id,
  s.scope,
  s.stream,
  s.brand_year,
  s.rate_bp,
  COALESCE(cp.term_start_date, CURRENT_DATE),
  'Seeded from build brief §6.4 default Scope B'
FROM channel_partners cp
CROSS JOIN (
  VALUES
    ('B', 'subscription',   1::smallint, 2700),
    ('B', 'subscription',   2::smallint, 1700),
    ('B', 'subscription',   3::smallint,  700),
    ('B', 'credit_overage', 1::smallint, 1700),
    ('B', 'credit_overage', 2::smallint, 1200),
    ('B', 'credit_overage', 3::smallint,  700),
    ('B', 'gmv_take',       1::smallint, 3000),
    ('B', 'gmv_take',       2::smallint, 2200),
    ('B', 'gmv_take',       3::smallint, 1200)
) AS s(scope, stream, brand_year, rate_bp)
WHERE cp.archetype = 'curated_marketplace'
  AND cp.legal_name ILIKE 'markato%'
ON CONFLICT (channel_partner_id, scope, stream, brand_year, effective_from)
DO NOTHING;

-- Markato Y1 cohort target. Bonus amount left at 0 — term-sheet v2 lands
-- the dollar figure; this row records the structural commitment (count +
-- window) so the cohort engine has something to evaluate against.
INSERT INTO partner_cohort_targets
  (channel_partner_id, label, target_brand_count, window_months,
   window_start_date, bonus_cents, status)
SELECT
  cp.id,
  'Y1 cohort target',
  20,
  12,
  COALESCE(cp.term_start_date, CURRENT_DATE),
  0,
  'open'
FROM channel_partners cp
WHERE cp.archetype = 'curated_marketplace'
  AND cp.legal_name ILIKE 'markato%'
  AND NOT EXISTS (
    SELECT 1 FROM partner_cohort_targets pct
    WHERE pct.channel_partner_id = cp.id
      AND pct.label = 'Y1 cohort target'
  );

-- DOWN (manual rollback only; the runner is UP-only):
-- DROP TABLE IF EXISTS partner_cohort_targets;
-- DROP TABLE IF EXISTS partner_rate_schedules;
-- ALTER TABLE channel_partners
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_billing_mode_supported,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_gmv_take_definition,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_active_rate_scope,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_gmv_take_rate_range,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_subsidy_cap_nonneg,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_clawback_days_positive,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_tail_months_positive,
--   DROP CONSTRAINT IF EXISTS ck_channel_partners_term_months_positive,
--   DROP COLUMN IF EXISTS monthly_overage_supported,
--   DROP COLUMN IF EXISTS prepaid_credits_supported,
--   DROP COLUMN IF EXISTS gmv_take_definition,
--   DROP COLUMN IF EXISTS active_rate_scope,
--   DROP COLUMN IF EXISTS gmv_take_rate_bp,
--   DROP COLUMN IF EXISTS per_brand_subsidy_cap_cents,
--   DROP COLUMN IF EXISTS nonpayment_clawback_days,
--   DROP COLUMN IF EXISTS churn_clawback_days,
--   DROP COLUMN IF EXISTS per_brand_tail_months,
--   DROP COLUMN IF EXISTS term_auto_renew,
--   DROP COLUMN IF EXISTS term_months,
--   DROP COLUMN IF EXISTS term_start_date;
-- ALTER TABLE channel_partners DROP CONSTRAINT IF EXISTS ck_channel_partners_archetype;
-- UPDATE channel_partners SET archetype = 'markato'
--   WHERE archetype = 'curated_marketplace' AND legal_name ILIKE 'markato%';
-- ALTER TABLE channel_partners
--   ADD CONSTRAINT ck_channel_partners_archetype CHECK (
--     archetype IN ('markato','agency','affiliate','platform','protocol_partner','other')
--   );
