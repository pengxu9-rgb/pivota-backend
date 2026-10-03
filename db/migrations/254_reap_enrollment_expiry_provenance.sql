-- Explicit malformed expiry must never be mistaken for optional omission.
ALTER TABLE IF EXISTS reap_agentic_enrollments
    ADD COLUMN IF NOT EXISTS hosted_url_expiry_invalid BOOLEAN NOT NULL DEFAULT FALSE;
