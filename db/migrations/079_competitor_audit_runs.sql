-- PR-2: persist competitor cohort audits.
--
-- When a parent audit (in merchant_audit_runs) names competitor
-- brands via category_visibility_test, the cohort orchestrator
-- enqueues a re-audit of each top-N competitor and writes results
-- here. Schema mirrors merchant_audit_runs but adds:
--   - parent_audit_run_id: links back to the merchant/prospect run
--     that triggered this competitor lookup
--   - competitor_brand: name as extracted from category competitors
--   - competitor_domain: resolved via Gemini grounded search
--     ("what's the official site for {brand}?")
--
-- Intentionally separate table from merchant_audit_runs:
--   - Different lifecycle (cohort runs are tied to a parent)
--   - Cleaner queries: cohort dashboard joins on parent_audit_run_id
--   - Avoids polluting merchant_audit_runs with competitor data when
--     downstream (trend deltas, scheduled re-audit) only cares about
--     real merchant runs.
--
-- Idempotent — safe to re-run.

CREATE TABLE IF NOT EXISTS competitor_audit_runs (
  run_id                        UUID PRIMARY KEY,
  parent_audit_run_id           UUID NOT NULL,
  competitor_brand              TEXT NOT NULL,
  competitor_domain             TEXT NULL,
  requested_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at                  TIMESTAMPTZ NULL,
  status                        TEXT NOT NULL,
  product_keys                  TEXT[] NULL,
  verdict_labels                TEXT[] NULL,
  visibility_score_avg          INTEGER NULL,
  attribution_score_avg         INTEGER NULL,
  category_visibility_score_avg INTEGER NULL,
  report_jsonb                  JSONB NULL,
  error_message                 TEXT NULL
);

-- Cohort lookup index: "give me all competitor runs for this parent".
CREATE INDEX IF NOT EXISTS idx_competitor_audit_runs_parent
    ON competitor_audit_runs (parent_audit_run_id, requested_at DESC);

-- Brand-history index: "show me prior cohort runs that audited this
-- specific competitor brand" (useful when the same brand surfaces as
-- a competitor across multiple parent audits).
CREATE INDEX IF NOT EXISTS idx_competitor_audit_runs_brand
    ON competitor_audit_runs (competitor_brand, requested_at DESC);
