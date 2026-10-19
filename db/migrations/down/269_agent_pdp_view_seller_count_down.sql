-- Older code never names this column, and readers treat its absence as NULL (they fall back to
-- offer_count), so a runtime rollback can leave it in place.
ALTER TABLE IF EXISTS agent_pdp_view
    DROP COLUMN IF EXISTS seller_count;
