-- Down for 298. Revert the code that reads and writes these columns first (the AgenPas internal read's A2A card and
-- the worker's `_record_payment_card`), or the next boot's self-heal adds them back.
ALTER TABLE IF EXISTS a2a_rail_purchases
  DROP COLUMN IF EXISTS card_method_id,
  DROP COLUMN IF EXISTS card_last4,
  DROP COLUMN IF EXISTS card_network;
