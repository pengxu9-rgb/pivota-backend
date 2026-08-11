-- TOMBSTONE (ADR-022, migration 125): the promotions table this migration
-- alters was dropped when the promotions lane was deleted. Guarded so a
-- fresh-provision run of the full migration sequence does not fail.
-- Shopify discount nodes can be open-ended. Preserve that semantics instead of
-- synthesizing a one-year end date in Pivota promotions.

ALTER TABLE IF EXISTS promotions
  ALTER COLUMN end_at DROP NOT NULL;

ALTER TABLE IF EXISTS promotions
  DROP CONSTRAINT IF EXISTS ck_promotions_time_window;

ALTER TABLE IF EXISTS promotions
  ADD CONSTRAINT ck_promotions_time_window
  CHECK (end_at IS NULL OR start_at < end_at);
