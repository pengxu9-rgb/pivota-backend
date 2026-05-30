-- Down for 145_decision_layer_protocol_dimension.sql

DROP INDEX IF EXISTS idx_agent_decision_funnel_links_protocol_merchant_created;

ALTER TABLE agent_decision_funnel_links
  DROP COLUMN IF EXISTS protocol;

ALTER TABLE checkout_decisions
  DROP COLUMN IF EXISTS protocol;

ALTER TABLE agent_decision_events
  DROP COLUMN IF EXISTS protocol;
