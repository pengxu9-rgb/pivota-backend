-- Reverses 290. Only the checkout path's lookup writes the column: a row read without it is never looked up,
-- so dropping it stops the lookups (the holds stay held for a human).
ALTER TABLE IF EXISTS a2a_rail_purchases DROP COLUMN IF EXISTS checkout_lookups;
