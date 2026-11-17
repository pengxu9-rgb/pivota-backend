-- Reverses 287. Only the checkout step reads the column: with the column gone, switch
-- A2A_RAIL_CHECKOUT_STEP_ENABLED off first, or a purchase being placed loses the task it is polled by.
ALTER TABLE IF EXISTS a2a_rail_purchases DROP COLUMN IF EXISTS checkout_task_id;
