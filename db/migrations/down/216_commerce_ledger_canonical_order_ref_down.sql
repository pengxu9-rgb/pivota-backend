DROP INDEX IF EXISTS idx_commerce_interactions_order_ref_unique;
DROP INDEX IF EXISTS ix_commerce_interactions_order_ref;
DROP INDEX IF EXISTS ix_commerce_interaction_events_order_ref;

ALTER TABLE commerce_interactions
  DROP COLUMN IF EXISTS order_ref;

ALTER TABLE commerce_interaction_events
  DROP COLUMN IF EXISTS order_ref;
