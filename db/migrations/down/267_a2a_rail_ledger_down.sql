-- Reverses 267. Refused while any parent row names the a2a rail: narrowing the CHECK would fail
-- on it, and dropping the child table would orphan it. Resolve those purchases first.
SET LOCAL lock_timeout = '5s';
ALTER TABLE agent_purchases DROP CONSTRAINT IF EXISTS ck_agent_purchases_rail;
ALTER TABLE agent_purchases ADD CONSTRAINT ck_agent_purchases_rail CHECK (rail IN ('reap'));
DROP TABLE IF EXISTS a2a_rail_purchases;
DROP TABLE IF EXISTS a2a_rail_buyer_refs;
