-- 187: acquisition-source attribution on merchant_onboarding.
-- Captured at registration (e.g. 'ai-readiness-audit' from the marketing-site
-- URL-capture funnel). Attribution only — never gates behavior. A runtime
-- backstop in db/merchant_onboarding.py (ensure_operating_mode_column) also
-- adds this column because prod skips the startup migration runner.
ALTER TABLE merchant_onboarding
    ADD COLUMN IF NOT EXISTS signup_source VARCHAR(64);
