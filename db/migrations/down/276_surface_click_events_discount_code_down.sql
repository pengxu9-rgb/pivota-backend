-- Reverts migration 276. Only after every image that names these columns on the surface_click_events
-- Table is gone; otherwise each select(surface_click_events) fails on UndefinedColumn. Same lock rule as
-- the up file: one transaction, bounded wait.
SET LOCAL lock_timeout = '3s';

ALTER TABLE surface_click_events
  DROP COLUMN IF EXISTS discount_code,
  DROP COLUMN IF EXISTS discount_carried;
