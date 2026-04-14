-- Shopify discount nodes can be open-ended. Preserve that semantics instead of
-- synthesizing a one-year end date in Pivota promotions.

ALTER TABLE promotions
  ALTER COLUMN end_at DROP NOT NULL;

ALTER TABLE promotions
  DROP CONSTRAINT IF EXISTS ck_promotions_time_window;

ALTER TABLE promotions
  ADD CONSTRAINT ck_promotions_time_window
  CHECK (end_at IS NULL OR start_at < end_at);
