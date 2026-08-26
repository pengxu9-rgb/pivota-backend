-- 202_external_seed_content_freshness.sql
--
-- When did we last re-read this seed's PRICE and AVAILABILITY?
--
-- Migration 200 fixed this question for the destination URL and stated the reason in
-- its own header: `updated_at` "measured 'when did we last write this row', never
-- 'when did we last see this URL'". The identical defect still governs the CONTENT
-- refresh queue. `get_external_referral_refresh_candidate_seed_ids`
-- (services/external_referral_readiness) picks its work with
--
--     ORDER BY updated_at ASC NULLS FIRST
--
-- so it re-fetches whatever was WRITTEN longest ago, which is not the same set as
-- whatever was CRAWLED longest ago. At least four subsystems bump `updated_at` on
-- rows that are actively served without going anywhere near the origin:
--
--   * services/external_seed_servability.py   -- sets attached_product_key
--   * services/identity_resolution.py         -- status flips
--   * services/pdp_governance_service.py      -- governance writes
--   * routes/employee_products.py             -- operator PATCHes
--
-- The first of those is the sharpest: the selector's primary query is
-- `WHERE attached_product_key IS NOT NULL ORDER BY updated_at ASC`, and attaching a
-- product key is precisely what bumps `updated_at`. A seed becoming servable — the
-- moment its price starts being quoted to buyers — sends it to the BACK of the queue
-- that exists to keep its price honest. Rows are starved in proportion to how much
-- attention the rest of the system pays them.
--
-- `last_crawled_at` is written ONLY on the success path of
-- `_refresh_external_seed_by_id`, which is reached only after a fetch that actually
-- reached the origin (`raise_on_unavailable=True`) for the URL we actually serve
-- (`_same_destination`). Nothing else may write it — that is the entire point, and it
-- is the same rule migration 200 set for `destination_checked_at`. A NULL means
-- "never crawled", which is why the index is NULLS FIRST: today that is every row.
--
-- This does NOT change what the refresh does, only which rows it reaches first. It is
-- the precondition for scheduling the job at all: `jobs/external_referral_refresh.py`
-- exists but no scheduler runs it, and arming it against the `updated_at` ordering
-- would systematically skip the rows most likely to be wrong while reporting success.

ALTER TABLE IF EXISTS external_product_seeds
  ADD COLUMN IF NOT EXISTS last_crawled_at TIMESTAMPTZ;

-- The refresh queue's work order: active seeds, least-recently-crawled first.
-- Partial on `status = 'active'` to match the selector's predicate, so the ordering
-- is served from the index rather than a sort over the whole table.
CREATE INDEX IF NOT EXISTS idx_external_product_seeds_last_crawled
  ON external_product_seeds (last_crawled_at NULLS FIRST)
  WHERE status = 'active';

COMMENT ON COLUMN external_product_seeds.last_crawled_at IS
  'When a fetch last re-read this seed''s price/availability FROM THE ORIGIN. Written only by the success path of _refresh_external_seed_by_id; never by a PATCH, a backfill, an attach, or updated_at. NULL = never crawled.';

-- Rollback (manual):
-- DROP INDEX IF EXISTS idx_external_product_seeds_last_crawled;
-- ALTER TABLE IF EXISTS external_product_seeds DROP COLUMN IF EXISTS last_crawled_at;
