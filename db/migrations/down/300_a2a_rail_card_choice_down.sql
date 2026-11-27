-- Down for 300. Revert the code that reads and writes these columns first (the worker's `_await_card_choice`, the
-- AgenPas internal read's `card_choice` and its card-choice write), or the next boot's self-heal adds them back.
ALTER TABLE IF EXISTS a2a_rail_purchases
  DROP COLUMN IF EXISTS card_confirmed_at,
  DROP COLUMN IF EXISTS card_options;
