-- PR-13: merchant self-service Agent Presence Monitoring configuration.
--
-- Merchant JWTs carry merchant_onboarding.merchant_id, so the APM
-- opt-in state lives on merchant_onboarding rather than the legacy
-- integer merchants table.
--
-- Idempotent — safe to re-run.

ALTER TABLE merchant_onboarding
    ADD COLUMN IF NOT EXISTS apm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS apm_cadence_days INTEGER NULL,
    ADD COLUMN IF NOT EXISTS apm_scope_jsonb JSONB NULL,
    ADD COLUMN IF NOT EXISTS apm_configured_at TIMESTAMPTZ NULL,
    ADD COLUMN IF NOT EXISTS apm_last_run_at TIMESTAMPTZ NULL;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'merchant_onboarding_apm_cadence_days_chk'
    ) THEN
        ALTER TABLE merchant_onboarding
            ADD CONSTRAINT merchant_onboarding_apm_cadence_days_chk
            CHECK (
                apm_cadence_days IS NULL
                OR apm_cadence_days IN (7, 14, 30)
            );
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_merchant_onboarding_apm_due
    ON merchant_onboarding (apm_last_run_at, apm_cadence_days)
    WHERE apm_enabled = TRUE;
