-- 099_domain_extractor_baselines.sql
--
-- Per-domain extraction quality baseline for regression detection.
-- Written by compute_domain_extractor_scorecards() inside
-- jobs/nightly_index_health_job.py.
--
-- "domain" is the apex domain of canonical_url in external_offer_snapshots
-- (e.g. "sephora.com", "cultbeauty.co.uk").
--
-- alert_state progression:
--   new        → first data collection pass; no prior baseline
--   ok         → all tracked fields within acceptable bounds vs. baseline
--   degraded   → at least one field dropped > 10% vs. baseline
--   regression → at least one field dropped > 20% vs. baseline
--
-- When alert_state becomes 'regression', the nightly job applies a second-pass
-- UPDATE that sets blocker_code='extractor_regression' on all index_pipeline_state
-- rows whose canonical_url maps to this domain.
--
-- Tracked fields: title, description, image_url, price
-- Coverage rate: fraction of sampled products with a non-empty value for the field.
-- Baseline is reset to current when alert_state recovers to 'ok'.

CREATE TABLE IF NOT EXISTS domain_extractor_baselines (
    domain              VARCHAR(256)    NOT NULL,

    -- {field_key: coverage_rate (0.0–1.0)}
    -- e.g. {"title": 0.98, "description": 0.72, "image_url": 0.95, "price": 0.88}
    baseline_coverage   JSONB           NOT NULL DEFAULT '{}',
    current_coverage    JSONB           NOT NULL DEFAULT '{}',

    -- Number of (seed, domain) pairs contributing to the most recent scorecard run
    sample_size         INT             NOT NULL DEFAULT 0,

    alert_state         VARCHAR(20)     NOT NULL DEFAULT 'new',

    -- Populated when alert_state is 'degraded' or 'regression'
    -- [{field, baseline, current, drop}]
    regression_details  JSONB           NOT NULL DEFAULT '[]',

    scorecard_version   VARCHAR(32),
    last_scored_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    -- Timestamp of the last baseline reset (happens on recovery to 'ok')
    baseline_set_at     TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    created_at          TIMESTAMPTZ     NOT NULL DEFAULT NOW(),

    CONSTRAINT domain_extractor_baselines_pkey PRIMARY KEY (domain),
    CONSTRAINT domain_extractor_baselines_alert_state_check
        CHECK (alert_state IN ('ok', 'degraded', 'regression', 'new'))
);

CREATE INDEX IF NOT EXISTS idx_deb_alert_state
    ON domain_extractor_baselines (alert_state)
    WHERE alert_state != 'ok';

CREATE INDEX IF NOT EXISTS idx_deb_last_scored
    ON domain_extractor_baselines (last_scored_at DESC);
