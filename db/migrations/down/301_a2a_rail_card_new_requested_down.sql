-- Down for 301. Revert the code that reads and writes this column first (the worker's new-card binding from the card
-- choice and the card-choice write's {"new_card": true}), or the next boot's self-heal adds it back.
ALTER TABLE IF EXISTS a2a_rail_purchases
  DROP COLUMN IF EXISTS card_new_requested_at;
