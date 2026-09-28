-- Reverses 246_catalog_offers_price_checked_at.sql. The gateway reads price_checked_at only while
-- CANONICAL_CATALOG_SERVED_PRICE_AS_OF is on: turn that off FIRST, or every canonical search query
-- fails on the missing column.
--
-- NOT STICKY ON ITS OWN: db/schema_guard.ensure_required_schema_light() re-adds the column and the
-- trigger on the next boot. Remove that heal in the same release if they must stay gone.
DROP TRIGGER IF EXISTS trg_catalog_offers_forget_unread_price_check ON catalog_offers;
DROP FUNCTION IF EXISTS catalog_offers_forget_unread_price_check();
ALTER TABLE catalog_offers DROP COLUMN IF EXISTS price_checked_at;
