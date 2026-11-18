-- Reverses 288. Only the checkout path's lock writes the column: switch A2A_RAIL_CHECKOUT_STEP_ENABLED
-- off first, or the lock step fails on every tick.
ALTER TABLE IF EXISTS a2a_rail_purchases DROP COLUMN IF EXISTS ceiling_source;
