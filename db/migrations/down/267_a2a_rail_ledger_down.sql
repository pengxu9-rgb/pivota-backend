-- Reverses 267. FAILS (the ADD CONSTRAINT errors, rolling back this file's transaction) while any
-- parent row names the a2a rail: resolve and delete those purchases first, or the child table would
-- be dropped under them.
SET LOCAL lock_timeout = '5s';
ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail;
ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap'));
DROP TABLE IF EXISTS a2a_rail_purchases;
DROP TABLE IF EXISTS a2a_rail_buyer_refs;
