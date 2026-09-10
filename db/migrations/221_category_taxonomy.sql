-- 221: ONE category vocabulary, in the database, because two repositories were each keeping their
-- own and calling the other's rows corrupt.
--
-- `category_path` on catalog_products is written and read by pivota-backend (CATEGORY_PATTERNS in
-- services/pdp_category_classifier.py) and by PIVOTA-Agent (src/services/beautyTaxonomy.js). Two
-- vocabularies over one column means every disagreement between them presents as data corruption
-- to whichever side is reading. Measured 2026-09-10: the gateway had written 315 rows to
-- `beauty/skincare/tone/toner` deliberately, this repo's taxonomy named `beauty/skincare/treat/
-- toner` and could not reach any of them, and this repo's own invariant reported them as
-- off-taxonomy. Industry standard (Google Product Taxonomy 5976, Shopify hb-3-2-9-17) sided with
-- the gateway. Nobody was wrong about the data; they were reading different dictionaries.
--
-- Three kinds of row, no type column needed:
--   canonical leaf   alias_of IS NULL, is_leaf = true    recall builds a prefix for it
--   interior node    alias_of IS NULL, is_leaf = false   an ancestor; reachable only via #2122
--   alias            alias_of IS NOT NULL                a spelling meaning another path
--
-- The self-FK is what stops an alias pointing at nothing — precisely how 117 orphan paths came to
-- exist. ONE HOP ONLY (enforced by the alias-is-not-a-leaf check plus the seeder's own assertion),
-- so resolution never loops and never needs a recursive CTE on a read path.
--
-- ⚠️ NOTE this file is NOT what creates the table on production. `web` deploys with
-- SKIP_HEAVY_STARTUP_INIT, so db/migrations/ never runs there; db/catalog.py's model plus
-- metadata.create_all builds it. This file is for environments that do run migrations, and for the
-- dialect gate. The two must agree — the MODEL decides, because create_all runs first and this
-- CREATE TABLE IF NOT EXISTS then skips.
--
-- SEEDING IS NOT HERE. Rows come from scripts/seed_category_taxonomy.py, which is idempotent and
-- reconciles both repos' vocabularies; a migration that seeded them would make the data un-editable
-- without a new migration, and this table is meant to be edited.
CREATE TABLE IF NOT EXISTS category_taxonomy (
  path        VARCHAR(255) PRIMARY KEY,
  label       VARCHAR(128) NOT NULL,
  is_leaf     BOOLEAN      NOT NULL DEFAULT false,
  alias_of    VARCHAR(255) NULL REFERENCES category_taxonomy(path),
  note        TEXT         NULL,
  updated_at  TIMESTAMPTZ  NOT NULL DEFAULT now(),
  CONSTRAINT ck_category_taxonomy_alias_is_not_a_leaf
    CHECK (alias_of IS NULL OR is_leaf = false),
  CONSTRAINT ck_category_taxonomy_alias_not_self
    CHECK (alias_of IS NULL OR alias_of <> path)
);

-- Resolution reads "give me every canonical leaf" and "resolve this spelling" on every load.
CREATE INDEX IF NOT EXISTS ix_category_taxonomy_leaf
  ON category_taxonomy (is_leaf) WHERE alias_of IS NULL;
