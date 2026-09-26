ALTER TABLE commerce_interaction_events
  DROP COLUMN IF EXISTS synthetic,
  DROP COLUMN IF EXISTS agent_identity_confidence,
  DROP COLUMN IF EXISTS authority,
  DROP COLUMN IF EXISTS write_path;
