-- Phase C-4 / PR-C: persisted merchant audit run history.
--
-- Replaces the in-memory rate-limit + history dict in
-- routes/merchant_audit_routes.py:_audit_run_history. That dict
-- lost state on every restart and didn't support trend / "audit
-- history" UX.
--
-- Each row records ONE invocation of POST /api/merchant-center/audit/
-- ai-commerce-readiness. The route inserts (status='running') at the
-- start, then updates (status='succeeded'/'failed', scores,
-- report_jsonb) at the end. count_runs_in_window() reads the table
-- to enforce the 2/24h cap; recent_runs_for_merchant() drives the
-- merchant_view.tracking.history_link payload.

CREATE TABLE IF NOT EXISTS merchant_audit_runs (
  run_id                       UUID PRIMARY KEY,
  merchant_id                  TEXT NOT NULL,
  requested_at                 TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  completed_at                 TIMESTAMPTZ NULL,
  status                       TEXT NOT NULL,         -- running | succeeded | failed
  product_keys                 TEXT[] NOT NULL,
  verdict_labels               TEXT[] NULL,
  visibility_score_avg         INTEGER NULL,
  attribution_score_avg        INTEGER NULL,
  category_visibility_score_avg INTEGER NULL,
  audited_via_pivota_canonical TEXT[] NULL,
  report_jsonb                 JSONB NULL,
  error_message                TEXT NULL
);

-- Window queries (rate limit + history pulls) all filter by merchant
-- + recent timestamp; this index covers both.
CREATE INDEX IF NOT EXISTS idx_merchant_audit_runs_merchant_window
  ON merchant_audit_runs (merchant_id, requested_at DESC);
