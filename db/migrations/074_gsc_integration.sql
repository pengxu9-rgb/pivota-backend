-- Phase D scaffolding: Google Search Console auto-submit data model.
--
-- Two new tables. NO production code paths read these yet — the
-- services.gsc_integration module is currently a stub returning
-- "not_configured" status until OAuth client credentials + a working
-- google-api-python-client wire-up land in a follow-up PR.
--
-- The audit pipeline READS gsc_oauth_tokens (presence-only) to decide
-- whether to surface "Grant Pivota GSC access" as an action. The
-- merchant_view.tracking.gsc_submission_status (also follow-up)
-- aggregates from gsc_url_submissions.
--
-- Why scaffold now: lets us ship the action surface + integration
-- state detection so merchants see the "next step" surface in the
-- audit. When OAuth credentials are configured, the wire-up is
-- additive (replace stub HTTP calls); no schema migration needed
-- after this one.

CREATE TABLE IF NOT EXISTS gsc_oauth_tokens (
  merchant_id        TEXT PRIMARY KEY,
  -- Refresh + access tokens are stored encrypted by the application
  -- layer (same encryption pattern as connector_credentials). Schema
  -- holds the ciphertext blob; never store plaintext.
  refresh_token_enc  TEXT NOT NULL,
  access_token_enc   TEXT NULL,
  access_token_expires_at TIMESTAMPTZ NULL,
  granted_scopes     TEXT[] NOT NULL,
  granted_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  -- Site URL the merchant authorized — typically their canonical
  -- store domain. We submit URLs scoped to this site only.
  authorized_site_url TEXT NOT NULL,
  -- Last time the token was successfully exchanged for a fresh
  -- access_token; used to gate health checks on stale tokens.
  last_refresh_ok_at TIMESTAMPTZ NULL,
  last_refresh_error TEXT NULL,
  revoked_at         TIMESTAMPTZ NULL
);

-- Per-URL submission state. One row per (merchant, URL); upserts
-- on each submit attempt + each status poll.
CREATE TABLE IF NOT EXISTS gsc_url_submissions (
  merchant_id        TEXT NOT NULL,
  url                TEXT NOT NULL,
  -- Submitted: Pivota told GSC about this URL. Indexed: GSC reports
  -- it's in their index. Pending: submitted but not yet indexed.
  -- Error: Submission rejected (URL not on authorized site, quota
  -- exceeded, GSC API error). Status mirrors GSC's URL Inspection
  -- terms when available.
  last_status        TEXT NOT NULL,                       -- submitted | pending | indexed | error | unknown
  last_status_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  submitted_at       TIMESTAMPTZ NULL,
  indexed_at         TIMESTAMPTZ NULL,
  error_message      TEXT NULL,
  -- The audit run that surfaced this URL as a submit candidate.
  -- Useful for "show me URLs queued by this audit" follow-ups.
  source_audit_run_id UUID NULL,
  PRIMARY KEY (merchant_id, url)
);

-- Used by `count_submitted_urls(merchant_id)` for the
-- merchant_view.tracking.gsc_submission_status aggregate.
CREATE INDEX IF NOT EXISTS idx_gsc_url_submissions_merchant_status
  ON gsc_url_submissions (merchant_id, last_status, last_status_at DESC);
