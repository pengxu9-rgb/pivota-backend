-- Reverse of 231_merchant_purchasability.sql.
--
-- DROPPING THIS TABLE RE-OPENS THE FLOWERBEAUTY HOLE, but only while the dial is on. With
-- MERCHANT_PURCHASABILITY_ENABLED off, no consumer reads the table and dropping it changes
-- nothing. With the dial ON, `is_purchasable` cannot find a fact and therefore answers False for
-- EVERY merchant — the Reap eligibility check then refuses `merchant_not_purchasable` across the
-- board and the checkout tier reports browse_only everywhere. That is fail-CLOSED, which is the
-- correct direction, but it is a full stop of the purchase rail rather than a rollback.
--
-- So: turn MERCHANT_PURCHASABILITY_ENABLED OFF FIRST, confirm no consumer is reading, and only
-- then run this. The facts are re-derivable — the sweep rebuilds them from live checkouts within
-- one TTL window — so nothing here is unrecoverable, unlike the operator-typed eligibility rows.
DROP INDEX IF EXISTS idx_merchant_purchasability_due;
DROP TABLE IF EXISTS merchant_purchasability;
