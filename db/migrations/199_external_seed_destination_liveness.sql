-- 199_external_seed_destination_liveness.sql
--
-- Record whether a seed's published destination URL still resolves.
--
-- Until now nothing in this system ever re-read a seed's `/products/<handle>`.
-- `stale_snapshot` (services/external_referral_readiness) approximated freshness from
-- `updated_at`, which ANY writer bumps — a PATCH, a backfill, or a refresh whose fetch
-- 404'd and fell back to the cached snapshot. It measured "when did we last write this
-- row", never "when did we last see this URL". Measured 2026-08-25: 10.4% of the
-- seeds whose brand catalogue we could read publish a link that is already broken
-- (docs/external-seed-dead-pdp-link-audit.md).
--
-- These four columns are written ONLY by a fetch that reached the origin. Nothing else
-- may touch them — that is the entire point. A NULL `destination_checked_at` means
-- "never verified", which is a blocker, not a pass.
--
-- `destination_failure_streak` counts CONSECUTIVE CONFIRMED-DEAD observations. A fetch
-- we could not complete (bot challenge, 429, 5xx, DNS, TLS, timeout, robots) is
-- `unverifiable` and must leave the streak untouched: 213 of 286 brand hosts refused
-- an audit client outright during the measurement above, so a reaper that treated
-- "cannot verify" as "dead" would have retired most of the corpus on its first run.

ALTER TABLE IF EXISTS external_product_seeds
  ADD COLUMN IF NOT EXISTS destination_checked_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS destination_http_status INTEGER,
  ADD COLUMN IF NOT EXISTS destination_verdict TEXT,
  ADD COLUMN IF NOT EXISTS destination_failure_streak INTEGER NOT NULL DEFAULT 0;

-- Honesty guard: the verdict vocabulary is closed. `live_delisted` and
-- `redirected_to_product` are NOT failures — the first is a product page the brand has
-- unlisted, the second is a rename we can repair by rewriting canonical_url.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ck_external_product_seeds_destination_verdict'
    ) THEN
        ALTER TABLE external_product_seeds
            ADD CONSTRAINT ck_external_product_seeds_destination_verdict
            CHECK (
                destination_verdict IS NULL
                OR destination_verdict IN (
                    'live',
                    'live_delisted',
                    'redirected_to_product',
                    'redirected_off_product',
                    'dead_404',
                    'unverifiable'
                )
            );
    END IF;
END $$;

-- The sweep's work queue: active seeds ordered by how long ago we last looked.
-- NULLS FIRST is the point — never-verified rows are the whole corpus today.
CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_checked
  ON external_product_seeds (destination_checked_at NULLS FIRST)
  WHERE status = 'active';

-- The serving gate's predicate: confirmed-dead rows, cheap to count for the dial.
CREATE INDEX IF NOT EXISTS idx_external_product_seeds_destination_verdict
  ON external_product_seeds (destination_verdict)
  WHERE destination_verdict IS NOT NULL;

COMMENT ON COLUMN external_product_seeds.destination_checked_at IS
  'When a fetch last REACHED the origin for this destination. Written only by an observation; never by a PATCH, a backfill, or updated_at. NULL = never verified.';
COMMENT ON COLUMN external_product_seeds.destination_http_status IS
  'HTTP status the origin answered on the last completed observation. NULL when the fetch never completed.';
COMMENT ON COLUMN external_product_seeds.destination_verdict IS
  'What a shopper following this link would get. See services/external_seed_destination_liveness.';
COMMENT ON COLUMN external_product_seeds.destination_failure_streak IS
  'Consecutive CONFIRMED-dead observations (dead_404 / redirected_off_product). Reset by any non-dead observation. NEVER incremented by an unverifiable one.';

-- Rollback (manual):
-- DROP INDEX IF EXISTS idx_external_product_seeds_destination_verdict;
-- DROP INDEX IF EXISTS idx_external_product_seeds_destination_checked;
-- ALTER TABLE IF EXISTS external_product_seeds DROP CONSTRAINT IF EXISTS ck_external_product_seeds_destination_verdict;
-- ALTER TABLE IF EXISTS external_product_seeds
--   DROP COLUMN IF EXISTS destination_failure_streak,
--   DROP COLUMN IF EXISTS destination_verdict,
--   DROP COLUMN IF EXISTS destination_http_status,
--   DROP COLUMN IF EXISTS destination_checked_at;
