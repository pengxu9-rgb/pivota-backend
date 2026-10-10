-- 260: the row's OWN NAME, folded once at write time, for the gateway's category browse (PIVOTA-Agent #2404).
--
-- WHY. The gateway's canonical catalog search (PIVOTA-Agent src/services/canonicalSearchQualitySql.js,
-- `identitySql`) folds a row's own name AT QUERY TIME -- middle dots removed, Latin accents folded,
-- lower-cased, every non-alphanumeric run collapsed to one space -- for two predicates on every
-- category browse: the own-name / form regex over every row the category index yields, and the
-- name-evidence carrier CTE over the whole table. Measured on prod 2026-10-10 (EXPLAIN ANALYZE,
-- BUFFERS, 'barrier moisturizer', one run, warm): 1.85 s, of which 1.04 s is that fold detoasting
-- product_payload (canonical_title / canonical_name) on all 4,807 category rows, and 0.40 s is the
-- carrier CTE's Seq Scan of 28k rows running the same fold. Under four concurrent calls (a deploy's
-- instances warming the buyer-market pool together) it stretched to 5-11 s and the primary to 40%.
--
-- WHAT. Two nullable TEXT columns on catalog_products, stamped by a BEFORE INSERT OR UPDATE trigger
-- with the SAME fold the gateway's SQL applies, over the same inputs:
--   name_norm      = fold(concat_ws(' ', title, product_type))
--                    -- what the carrier CTE folds (searchNameEvidence: own name is title +
--                    -- product_type only; the LIKE prefilter and the regex both read this).
--   own_name_norm  = fold(concat_ws(' ', title, product_type,
--                                    product_payload->>'canonical_title', product_payload->>'canonical_name'))
--                    -- what the category WHERE's own-name / form / conflicting-type regexes fold.
-- The fold lives in ONE SQL function, catalog_products_identity_fold(text), byte-for-byte the
-- gateway's identitySql, so the equality `own_name_norm = identitySql(...)` is a test, not a hope
-- (tests/test_migration_260_catalog_products_own_name_norm.py pins the expression text against the
-- gateway's and, on Postgres, the column against the live expression over accented / dotted names).
--
-- The trigger recomputes BOTH columns on EVERY insert and update (not only when the inputs change):
-- a row schema_guard pre-created the columns on (NULL) is repaired by its next write, and the cost is
-- two regexp_replace calls per row write. EXISTING ROWS ARE NOT BACKFILLED HERE: that is
-- scripts/backfill_catalog_products_name_norm.py -- bounded, resumable, throttled batches that touch
-- each unstamped row so THIS trigger stamps it (the script never restates the fold), dry-run by
-- default, run by hand with the primary under 35%. This file stays a short transaction: function,
-- columns, trigger.
--
-- READERS. Nothing reads these columns until the gateway does (a flagged change in
-- canonicalSearchQualitySql.js, with today's expression as the fallback for NULL -- a row that
-- schema_guard created the column on and no write has touched yet). The trigram index that serves
-- the carrier CTE's LIKE prefilter is migration 261 (built outside a transaction, so its own file:
-- the runner sends any file that names that build mode statement-at-a-time on autocommit, and THIS
-- file must run as ONE transaction -- function, trigger and backfill together -- so that word stays out of it).
--
-- Columns are registered in db/catalog.py and db/schema_guard.py (REQUIRED_SCHEMA + the self-heal
-- ALTER): schema_guard lands the COLUMNS on a prod boot; this migration lands the function and the
-- trigger and must be applied by hand (db/migrations do not self-apply in prod); the script backfills.

CREATE OR REPLACE FUNCTION catalog_products_identity_fold(expression TEXT)
RETURNS TEXT
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
AS $$
  SELECT trim(regexp_replace(lower(translate(regexp_replace(coalesce(expression, ''), '[·•]', '', 'g'), 'ÀÁÂÃÄÅÈÉÊËÌÍÎÏÒÓÔÕÖÙÚÛÜÝàáâãäåèéêëìíîïòóôõöùúûüýÿ', 'AAAAAAEEEEIIIIOOOOOUUUUYaaaaaaeeeeiiiiooooouuuuyy')), '[^[:alnum:]]+', ' ', 'g'))
$$;

ALTER TABLE catalog_products
  ADD COLUMN IF NOT EXISTS name_norm TEXT,
  ADD COLUMN IF NOT EXISTS own_name_norm TEXT;

CREATE OR REPLACE FUNCTION catalog_products_stamp_name_norm()
RETURNS TRIGGER AS $$
BEGIN
  NEW.name_norm := catalog_products_identity_fold(concat_ws(' ', NEW.title, NEW.product_type));
  NEW.own_name_norm := catalog_products_identity_fold(
    concat_ws(' ', NEW.title, NEW.product_type,
              NEW.product_payload->>'canonical_title', NEW.product_payload->>'canonical_name'));
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_catalog_products_stamp_name_norm ON catalog_products;
CREATE TRIGGER trg_catalog_products_stamp_name_norm
  BEFORE INSERT OR UPDATE ON catalog_products
  FOR EACH ROW
  EXECUTE FUNCTION catalog_products_stamp_name_norm();
