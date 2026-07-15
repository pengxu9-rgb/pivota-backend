-- Wave-3 B2: read-only share links for completed URL-audit runs.
-- One row per issued token; revocation is a soft flag so links can be
-- disabled instantly without deleting the audit trail.
CREATE TABLE IF NOT EXISTS audit_share_tokens (
    token        TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL,
    merchant_id  TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at   TIMESTAMPTZ NOT NULL,
    revoked_at   TIMESTAMPTZ NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_share_tokens_run
    ON audit_share_tokens (run_id);
