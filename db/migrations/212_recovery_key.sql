-- The join the Prove stage was missing.
--
-- commerce_interactions already records a complete agent loop on one row —
-- prompt_id, result_id, click_id, cart_id, checkout_id, order_id — so a
-- conversion is fully observable. What it could not say is WHY the buyer
-- came: no column referred to the finding or the action that preceded them.
-- A conversion could be seen and never attributed to the fix that caused it,
-- which is the difference between "Prove" and a coincidence.
--
-- merchant_tasks.recovery_key holds the handle, derived by
-- db.merchant_tasks.recovery_key_for from the canonical action identity that
-- already survives re-audits. commerce_interactions.recovery_key receives it
-- from the attributed link the agent followed.
--
-- Both NULLABLE and both stay that way. Rows written before this column
-- existed have no key, and an absent key means "not attributable" — which is
-- honest. Backfilling one by guessing would attribute outcomes to actions
-- that may not have caused them, which is worse than not answering.

ALTER TABLE merchant_tasks
  ADD COLUMN IF NOT EXISTS recovery_key TEXT NULL;

ALTER TABLE commerce_interactions
  ADD COLUMN IF NOT EXISTS recovery_key VARCHAR(40) NULL;

-- The lookup this exists for: given a recovery key, did anything convert?
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_recovery_key
  ON commerce_interactions (recovery_key)
  WHERE recovery_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_merchant_tasks_recovery_key
  ON merchant_tasks (merchant_id, recovery_key)
  WHERE recovery_key IS NOT NULL;
