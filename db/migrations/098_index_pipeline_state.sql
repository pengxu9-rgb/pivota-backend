-- 098_index_pipeline_state.sql
--
-- Durable per-product pipeline stage machine. One row per canonical product
-- keyed on content_key (from catalog_products, mig 083). Written exclusively
-- by jobs/nightly_index_health_job.py. Never written by the serving path.
--
-- Stage progression (highest satisfied wins):
--   discovered    → catalog_products row with content_key exists
--   crawled       → external_offer_snapshots row exists for the domain
--   extracted     → seed_data non-empty with title present
--   quality_gated → content_quality_score >= QUALITY_SCORE_THRESHOLD AND has_image
--                   AND has_price
--                   AND description_length >= 50
--   shadow_indexed → serving_eligible=TRUE AND agent_pdp_view row exists
--   public_indexed → serving_eligible=TRUE AND apv.pdp_lifecycle_stage='published'
--
-- THE THRESHOLD LIVES IN CODE, NOT HERE. This header said 65 until 2026-08-11;
-- the real bar has been services.index_pipeline_state_service.QUALITY_SCORE_THRESHOLD (71.4 since 2026-07-28, #1612)
-- since it was raised from 65.0 IN LOCKSTEP with dropping the dead `summary`
-- component from the scorer (7 terms -> 6, so every score rescales by 7/6 —
-- holding 65 would have been a ~16% widening). A stale number here is not
-- harmless: it sent a 2026-08-11 investigation looking for a threshold bug that
-- did not exist. Read the constant.
--
-- blocker_code values (first failing check wins):
--   not_live          — catalog_products.sync_status is not 'live'
--   non_core_product  — sample/gift/protection/GWP row not eligible for commerce index
--   no_seed           — no external_product_seeds row linked to this content_key
--   no_extraction     — seed_data null or title missing
--   low_quality       — content_quality_score < QUALITY_SCORE_THRESHOLD
--   no_image          — image_url null/empty after extraction
--   no_price          — no catalog_offers row with list_price > 0
--   short_description — description present but < 50 chars after strip
--   entity_unresolved — no product_group_members row AND pdp_scope != 'canonical'
--   seed_audit_fail   — seed_data.review_summary.review_status = 'issues_unresolved'
--   extractor_regression — domain in domain_extractor_baselines with alert_state='regression'
--   none              — no blocker; product is at its natural stage
--
-- seed_audit_status values:
--   no_issues_found | auto_corrected | issues_unresolved | not_audited

CREATE TABLE IF NOT EXISTS index_pipeline_state (
    -- Identity
    content_key                 VARCHAR(40)  NOT NULL,
    pivota_signature_id         VARCHAR(40),
    merchant_id                 VARCHAR(100),

    -- Stage machine
    pipeline_stage              VARCHAR(20)  NOT NULL DEFAULT 'discovered',

    -- Blocker
    blocker_code                VARCHAR(40)  NOT NULL DEFAULT 'none',
    blocker_detail              TEXT,

    -- Serving gate
    serving_eligible            BOOLEAN      NOT NULL DEFAULT FALSE,

    -- Quality signals (denormalized from product_quality_snapshot)
    content_quality_score       REAL,
    model_readiness_score       REAL,
    conversion_potential_score  REAL,
    quality_rules_version       VARCHAR(32),
    quality_scored_at           TIMESTAMPTZ,

    -- Extraction state
    -- sha256(title || '::' || description || '::' || image_url) from agent_pdp_view
    extraction_content_hash     VARCHAR(64),
    extractor_version           VARCHAR(32),
    last_extracted_at           TIMESTAMPTZ,

    -- Consolidated sub-statuses
    seed_audit_status           VARCHAR(30),
    identity_resolved           BOOLEAN      NOT NULL DEFAULT FALSE,
    product_group_id            VARCHAR(64),

    -- Cheap field-presence checks used for serving eligibility
    has_image                   BOOLEAN      NOT NULL DEFAULT FALSE,
    has_price                   BOOLEAN      NOT NULL DEFAULT FALSE,
    description_length          INT,

    -- Job metadata
    consolidation_version       VARCHAR(32),
    last_consolidated_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT NOW(),

    CONSTRAINT index_pipeline_state_pkey PRIMARY KEY (content_key),
    CONSTRAINT index_pipeline_state_stage_check
        CHECK (pipeline_stage IN (
            'discovered', 'crawled', 'extracted',
            'quality_gated', 'shadow_indexed', 'public_indexed'
        ))
);

CREATE INDEX IF NOT EXISTS idx_ips_serving_eligible
    ON index_pipeline_state (serving_eligible)
    WHERE serving_eligible = TRUE;

CREATE INDEX IF NOT EXISTS idx_ips_pipeline_stage
    ON index_pipeline_state (pipeline_stage, blocker_code);

CREATE INDEX IF NOT EXISTS idx_ips_merchant_stage
    ON index_pipeline_state (merchant_id, pipeline_stage)
    WHERE merchant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ips_last_consolidated
    ON index_pipeline_state (last_consolidated_at DESC);

CREATE INDEX IF NOT EXISTS idx_ips_pivota_signature
    ON index_pipeline_state (pivota_signature_id)
    WHERE pivota_signature_id IS NOT NULL;
