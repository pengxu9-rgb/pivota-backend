-- Older code never names market_prices; the legacy columns it reads are written unchanged, so a
-- runtime rollback can leave the column in place. Drop it only with the writers' flag off.
ALTER TABLE IF EXISTS agent_pdp_view DROP COLUMN IF EXISTS market_prices;
