-- Migration 040: Reviews Center (SKU Reviews + Seller Feedback + Groups + Featured + Imports + Audit)
-- PostgreSQL only.
--
-- Notes:
-- - This migration is written to be idempotent (CREATE TABLE/INDEX IF NOT EXISTS).
-- - Some deployments may not have `users` / `employees` tables; ALTERs are guarded with IF EXISTS.
--
-- Key rules:
-- - product_key = "{merchant_id}|{platform}|{platform_product_id}"
-- - sku_key     = "{product_key}|{variant_id or '∅'}"
--
-- ---------------------------------------------------------------------------
-- Permissions (tag-based) for employee governance (best-effort)
-- ---------------------------------------------------------------------------

ALTER TABLE IF EXISTS users
  ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE IF EXISTS employees
  ADD COLUMN IF NOT EXISTS permissions JSONB NOT NULL DEFAULT '[]'::jsonb;

-- ---------------------------------------------------------------------------
-- Soft Canonical review groups
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS review_group (
  id              BIGSERIAL PRIMARY KEY,
  group_type      TEXT NOT NULL, -- GTIN | BRAND_MPN | MANUAL
  group_key       TEXT NOT NULL, -- gtin:008... | mpn:brand|mpn | manual:<id>
  confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  status          TEXT NOT NULL DEFAULT 'active', -- active | disabled
  created_by      TEXT NOT NULL DEFAULT 'system', -- system | employee
  created_by_employee_id TEXT NULL,
  featured_frozen BOOLEAN NOT NULL DEFAULT FALSE,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_review_group_group_key ON review_group (group_key);
CREATE INDEX IF NOT EXISTS idx_review_group_status ON review_group (status);


CREATE TABLE IF NOT EXISTS review_group_membership (
  id              BIGSERIAL PRIMARY KEY,
  group_id        BIGINT NOT NULL REFERENCES review_group(id) ON DELETE CASCADE,
  product_key     TEXT NOT NULL,
  sku_key         TEXT NOT NULL,
  merchant_id     TEXT NOT NULL,
  platform        TEXT NOT NULL,
  platform_product_id TEXT NOT NULL,
  variant_id      TEXT NULL,
  match_type      TEXT NOT NULL, -- GTIN | BRAND_MPN | MANUAL
  evidence        JSONB NULL,
  confidence      DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  status          TEXT NOT NULL DEFAULT 'active', -- active | removed
  created_by      TEXT NOT NULL DEFAULT 'system', -- system | employee
  created_by_employee_id TEXT NULL,
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- A sku_key can belong to at most one active group.
CREATE UNIQUE INDEX IF NOT EXISTS ux_review_group_membership_active_sku
  ON review_group_membership (sku_key)
  WHERE status = 'active';

CREATE INDEX IF NOT EXISTS idx_review_group_membership_group_id ON review_group_membership (group_id);
CREATE INDEX IF NOT EXISTS idx_review_group_membership_product_key ON review_group_membership (product_key);


-- ---------------------------------------------------------------------------
-- Imported identities (shadow/unclaimed users)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS external_identities (
  id                BIGSERIAL PRIMARY KEY,
  merchant_id        TEXT NOT NULL,
  source_system      TEXT NOT NULL,
  external_user_id   TEXT NULL,
  author_fingerprint TEXT NULL,
  display_name       TEXT NULL,
  status             TEXT NOT NULL DEFAULT 'unclaimed', -- unclaimed | claimed
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Uniqueness when external_user_id is present.
CREATE UNIQUE INDEX IF NOT EXISTS ux_external_identities_external_user
  ON external_identities (merchant_id, source_system, external_user_id)
  WHERE external_user_id IS NOT NULL AND external_user_id <> '';

CREATE INDEX IF NOT EXISTS idx_external_identities_merchant_source ON external_identities (merchant_id, source_system);


-- ---------------------------------------------------------------------------
-- Product reviews (SKU-level, but variant_id may be missing -> '∅' sentinel in sku_key)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS product_reviews (
  id                 BIGSERIAL PRIMARY KEY,
  product_key         TEXT NOT NULL,
  sku_key             TEXT NOT NULL,
  merchant_id         TEXT NOT NULL,
  platform            TEXT NOT NULL,
  platform_product_id TEXT NOT NULL,
  variant_id          TEXT NULL,
  group_id            BIGINT NULL REFERENCES review_group(id) ON DELETE SET NULL,

  author_user_id      BIGINT NULL REFERENCES external_identities(id) ON DELETE SET NULL,
  source_type         TEXT NOT NULL DEFAULT 'imported', -- native | imported
  source_system       TEXT NULL,
  external_review_id  TEXT NULL,
  dedupe_key          TEXT NULL,

  verification        TEXT NOT NULL DEFAULT 'unverified', -- verified_purchase | partner_verified | unverified
  rating              SMALLINT NULL,
  title               TEXT NULL,
  body                TEXT NULL,
  body_redacted       TEXT NULL,
  redaction           JSONB NULL,
  editor_note         TEXT NULL,

  media_count         INTEGER NOT NULL DEFAULT 0,
  risk_flags          JSONB NULL,
  status              TEXT NOT NULL DEFAULT 'active', -- active | folded | removed | under_review

  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_product_reviews_sku_created ON product_reviews (sku_key, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_reviews_group_created ON product_reviews (group_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_reviews_merchant_created ON product_reviews (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_product_reviews_status ON product_reviews (status);

-- Legacy dedupe index (replaced by 041 hardening, but keep for older runners).
CREATE UNIQUE INDEX IF NOT EXISTS ux_product_reviews_source_external
  ON product_reviews (source_system, external_review_id)
  WHERE external_review_id IS NOT NULL AND external_review_id <> '';


CREATE TABLE IF NOT EXISTS media_assets (
  id          BIGSERIAL PRIMARY KEY,
  review_id   BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  public_id   TEXT NULL,
  type        TEXT NOT NULL, -- image | video
  url         TEXT NOT NULL,
  file_path   TEXT NULL,
  width       INTEGER NULL,
  height      INTEGER NULL,
  file_hash   TEXT NULL,
  status      TEXT NOT NULL DEFAULT 'active', -- active | removed
  created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_media_assets_review_id ON media_assets (review_id);


CREATE TABLE IF NOT EXISTS review_replies (
  id            BIGSERIAL PRIMARY KEY,
  review_id     BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  replier_type  TEXT NOT NULL, -- merchant | official
  replier_id    TEXT NOT NULL, -- merchant_id or employee_id
  body          TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_review_replies_review_id_created ON review_replies (review_id, created_at);


CREATE TABLE IF NOT EXISTS review_interactions (
  id         BIGSERIAL PRIMARY KEY,
  review_id  BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  user_id    TEXT NOT NULL,
  type       TEXT NOT NULL, -- helpful | report
  value      INTEGER NOT NULL DEFAULT 1,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_review_interactions_user
  ON review_interactions (review_id, user_id, type);


-- ---------------------------------------------------------------------------
-- Seller feedback (merchant-level only)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS seller_feedback (
  id              BIGSERIAL PRIMARY KEY,
  merchant_id      TEXT NOT NULL,
  order_ref        JSONB NULL,
  rating_overall   SMALLINT NULL,
  dims_json        JSONB NULL,
  body             TEXT NULL,
  status           TEXT NOT NULL DEFAULT 'active', -- active | folded | removed | under_review
  created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_seller_feedback_merchant_created ON seller_feedback (merchant_id, created_at DESC);


-- ---------------------------------------------------------------------------
-- Featured reviews (unboxing picks)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS review_featured (
  id           BIGSERIAL PRIMARY KEY,
  group_id     BIGINT NOT NULL REFERENCES review_group(id) ON DELETE CASCADE,
  review_id    BIGINT NOT NULL REFERENCES product_reviews(id) ON DELETE CASCADE,
  rank         INTEGER NOT NULL DEFAULT 0,
  score        DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  reason_tags  JSONB NULL,
  generated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  is_pinned    BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_review_featured_group_review ON review_featured (group_id, review_id);
CREATE INDEX IF NOT EXISTS idx_review_featured_group_rank ON review_featured (group_id, rank);


-- ---------------------------------------------------------------------------
-- Import pipeline (employee-only)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS import_batches (
  id                     BIGSERIAL PRIMARY KEY,
  merchant_id             TEXT NOT NULL,
  source_system           TEXT NOT NULL,
  status                 TEXT NOT NULL DEFAULT 'created', -- created | uploaded | validated | committed | failed
  created_by_employee_id  TEXT NULL,
  reviews_file_path       TEXT NULL,
  media_zip_path          TEXT NULL,
  report_json             JSONB NULL,
  created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_import_batches_merchant_created ON import_batches (merchant_id, created_at DESC);


CREATE TABLE IF NOT EXISTS import_items (
  id                    BIGSERIAL PRIMARY KEY,
  batch_id               BIGINT NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
  merchant_id            TEXT NOT NULL,
  source_system          TEXT NOT NULL,
  external_review_id     TEXT NULL,
  external_user_id       TEXT NULL,
  payload_json           JSONB NOT NULL,
  match_product_key      TEXT NULL,
  match_sku_key          TEXT NULL,
  match_confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  group_id               BIGINT NULL REFERENCES review_group(id) ON DELETE SET NULL,
  group_confidence       DOUBLE PRECISION NOT NULL DEFAULT 0.0,
  dedupe_key             TEXT NULL,
  status                 TEXT NOT NULL DEFAULT 'pending', -- pending | matched | downgraded_to_product_level | rejected | imported
  error_reason           TEXT NULL,
  created_at             TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at             TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_import_items_source_external_review
  ON import_items (source_system, external_review_id)
  WHERE external_review_id IS NOT NULL AND external_review_id <> '';

CREATE INDEX IF NOT EXISTS idx_import_items_batch_status ON import_items (batch_id, status);


-- ---------------------------------------------------------------------------
-- Employee audit log (reviews governance)
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS employee_audit_logs (
  id                 BIGSERIAL PRIMARY KEY,
  actor_employee_id   TEXT NULL,
  actor_email         TEXT NULL,
  action              TEXT NOT NULL,
  target_type         TEXT NOT NULL,
  target_id           TEXT NOT NULL,
  reason              TEXT NULL,
  before_json         JSONB NULL,
  after_json          JSONB NULL,
  created_at          TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_employee_audit_logs_target ON employee_audit_logs (target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_employee_audit_logs_created ON employee_audit_logs (created_at DESC);
