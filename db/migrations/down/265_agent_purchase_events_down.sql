-- Down for 265: drop the purchase event table. Undelivered events are lost; delivered ones stay in
-- agent_webhook_deliveries.
DROP INDEX IF EXISTS idx_agent_purchase_events_status_created;
DROP INDEX IF EXISTS uq_agent_purchase_events_purchase_state;
DROP TABLE IF EXISTS agent_purchase_events;
