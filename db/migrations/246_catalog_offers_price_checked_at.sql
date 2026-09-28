-- 246_catalog_offers_price_checked_at.sql
--
-- WHEN THIS OFFER'S PRICE WAS LAST READ. The gateway publishes it on search cards as
-- `price_as_of` (PIVOTA-Agent #2322), so it has to mean exactly that.
--
-- `updated_at` does not. It is a row-write stamp, set to NOW() by suppression, the
-- currency/market backfills, the vocabulary repairs, the dedupe merges, and a seed_data merge
-- that re-read no price. Measured on prod 2026-09-28 over the 59,877 served offers:
--   * 50,776 (85%) share their updated_at minute with at least 100 other rows;
--   * 22,530 had their seed's price re-read (`last_crawled_at`) AFTER their last stamp.
--
-- WHO STAMPS IT. A writer that read the price, and nothing else:
--   * the attached-listing lane (services/external_offer_dual_write, #2416), which runs only on
--     a refresh that re-read the price and its currency, or on an employee's price edit;
--   * the mirror upsert, when its caller vouches for the same read.
-- NULL means "no read on record". It is never defaulted, and never backfilled from updated_at.
--
-- WHAT KEEPS IT TRUE. A stamp dates ONE price. The trigger below forgets it when any writer
-- moves list_price, merchant_effective_price or currency without stamping a read of its own.
-- That covers the ingest upserts, the one-off repair scripts, and every writer yet to be
-- written, instead of a rule restated in each of them.
--
-- PROD. Numbered migrations do not self-apply in prod. db/schema_guard.py heals the column
-- and installs this trigger on boot, using the statements read from this file.

ALTER TABLE catalog_offers ADD COLUMN IF NOT EXISTS price_checked_at TIMESTAMPTZ NULL;

COMMENT ON COLUMN catalog_offers.price_checked_at IS
  'When this price was last read from its source. Stamped only by a writer that read it; '
  'cleared by trg_catalog_offers_forget_unread_price_check when the price or currency moves '
  'without a read. NULL = no read on record. Not updated_at.';

CREATE OR REPLACE FUNCTION catalog_offers_forget_unread_price_check()
RETURNS TRIGGER AS $$
BEGIN
    IF NEW.price_checked_at IS NOT DISTINCT FROM OLD.price_checked_at THEN
        NEW.price_checked_at := NULL;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_catalog_offers_forget_unread_price_check ON catalog_offers;

CREATE TRIGGER trg_catalog_offers_forget_unread_price_check
    BEFORE UPDATE OF list_price, merchant_effective_price, currency ON catalog_offers
    FOR EACH ROW
    WHEN (
        OLD.list_price IS DISTINCT FROM NEW.list_price
        OR OLD.merchant_effective_price IS DISTINCT FROM NEW.merchant_effective_price
        OR OLD.currency IS DISTINCT FROM NEW.currency
    )
    EXECUTE FUNCTION catalog_offers_forget_unread_price_check();
