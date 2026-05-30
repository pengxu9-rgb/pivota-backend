-- 145_decision_layer_protocol_dimension.sql
--
-- Add a protocol dimension to decision-layer attribution rows. Existing PDP
-- direct traffic remains the default while future protocol adapters reserve
-- explicit identifiers in application code.

ALTER TABLE agent_decision_events
  ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'pdp_direct';

ALTER TABLE checkout_decisions
  ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'pdp_direct';

ALTER TABLE agent_decision_funnel_links
  ADD COLUMN IF NOT EXISTS protocol VARCHAR(32) NOT NULL DEFAULT 'pdp_direct';

CREATE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_protocol_merchant_created
  ON agent_decision_funnel_links (protocol, merchant_id, created_at DESC)
  WHERE merchant_id IS NOT NULL;
