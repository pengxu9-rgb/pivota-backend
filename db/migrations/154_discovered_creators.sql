-- 154_discovered_creators.sql
-- Data-driven creator directory (Phase 3 — the "creator API" data layer).
--
-- The creator MATCHER (services/creator_matcher.py), scoring, the
-- creator_partnership action, and the MatchedCreatorCard UI all already exist.
-- The only reason creator matching returns no_data is that the directory
-- (data/creator_database.json) is empty. This table makes the directory
-- data-driven + accumulating: creators land here from any source (BD curation,
-- an external creator-discovery API, or a harvest) and the matcher reads them
-- immediately. Category-indexed so a merchant's category resolves to creators.
--
-- Idempotent. Prod skips the migration runner — apply via railway ssh / admin.
BEGIN;

CREATE TABLE IF NOT EXISTS discovered_creators (
    creator_id            TEXT PRIMARY KEY,             -- stable slug, e.g. "tiktok:glowwithval"
    display_name          TEXT,
    platform              TEXT,                         -- instagram | tiktok | youtube | ...
    platform_url          TEXT,
    category_tags         JSONB NOT NULL DEFAULT '[]'::jsonb,  -- merchant categories they cover
    audience_size_band    TEXT,                         -- micro | small | medium | large
    recent_coverage       JSONB NOT NULL DEFAULT '[]'::jsonb,  -- competitor brands covered
    contact_method        TEXT,                         -- submission_form | direct_email | dm
    contact_url           TEXT,
    sample_brief_template TEXT,
    source                TEXT,                         -- bd_curated | external_api | harvest
    last_verified_at      TIMESTAMPTZ,
    created_at            TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- The matcher reads by category membership (category_tags @> ['skincare']).
CREATE INDEX IF NOT EXISTS idx_discovered_creators_category
  ON discovered_creators USING GIN (category_tags);

COMMIT;
