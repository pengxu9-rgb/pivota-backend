-- Migration 053: buyer review binding supports one review per paid order.
-- PostgreSQL only.

ALTER TABLE buyer_review_user_subject
  ADD COLUMN IF NOT EXISTS order_id TEXT;

-- Legacy schema had one-row-per-subject unique constraint.
ALTER TABLE buyer_review_user_subject
  DROP CONSTRAINT IF EXISTS ux_buyer_review_user_subject;

-- In case the old uniqueness was created as an index (not a table constraint).
DROP INDEX IF EXISTS ux_buyer_review_user_subject;

CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_order
  ON buyer_review_user_subject (user_id, subject_type, subject_id, order_id);

-- Preserve backward compatibility for historical rows where order_id is NULL.
CREATE UNIQUE INDEX IF NOT EXISTS ux_buyer_review_user_subject_legacy_null_order
  ON buyer_review_user_subject (user_id, subject_type, subject_id)
  WHERE order_id IS NULL;

CREATE INDEX IF NOT EXISTS idx_buyer_review_user_subject_order_id
  ON buyer_review_user_subject (order_id);
