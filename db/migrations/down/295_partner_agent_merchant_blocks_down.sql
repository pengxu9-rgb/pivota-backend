-- Reverses 295. DESTROYS the record of every challenge and block, cleared or open: export the
-- table first if any row was ever written. Nothing recreates it at the next start (it has no
-- startup heal). After this the read answers "cannot tell" for every merchant, which a filter
-- treats as not clear.
DROP INDEX IF EXISTS idx_partner_agent_merchant_blocks_open;
DROP INDEX IF EXISTS uq_partner_agent_merchant_blocks_purchase;
DROP TABLE IF EXISTS partner_agent_merchant_blocks;
