ALTER TABLE IF EXISTS agent_usage_logs
  ADD COLUMN IF NOT EXISTS caller_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS source_channel VARCHAR(128),
  ADD COLUMN IF NOT EXISTS source_family VARCHAR(64),
  ADD COLUMN IF NOT EXISTS query_source VARCHAR(128),
  ADD COLUMN IF NOT EXISTS protocol_name VARCHAR(64),
  ADD COLUMN IF NOT EXISTS commerce_surface VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_model VARCHAR(128);

ALTER TABLE IF EXISTS surface_click_events
  ADD COLUMN IF NOT EXISTS commerce_surface VARCHAR(64),
  ADD COLUMN IF NOT EXISTS source_channel VARCHAR(128),
  ADD COLUMN IF NOT EXISTS source_family VARCHAR(64),
  ADD COLUMN IF NOT EXISTS query_source VARCHAR(128),
  ADD COLUMN IF NOT EXISTS agent_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS protocol_name VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_model VARCHAR(128),
  ADD COLUMN IF NOT EXISTS caller_id VARCHAR(128);

ALTER TABLE IF EXISTS commerce_attribution_edges
  ADD COLUMN IF NOT EXISTS commerce_surface VARCHAR(64),
  ADD COLUMN IF NOT EXISTS source_channel VARCHAR(128),
  ADD COLUMN IF NOT EXISTS source_family VARCHAR(64),
  ADD COLUMN IF NOT EXISTS query_source VARCHAR(128),
  ADD COLUMN IF NOT EXISTS agent_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS protocol_name VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_model VARCHAR(128),
  ADD COLUMN IF NOT EXISTS caller_id VARCHAR(128);

ALTER TABLE IF EXISTS commerce_interactions
  ADD COLUMN IF NOT EXISTS commerce_surface VARCHAR(64),
  ADD COLUMN IF NOT EXISTS source_channel VARCHAR(128),
  ADD COLUMN IF NOT EXISTS source_family VARCHAR(64),
  ADD COLUMN IF NOT EXISTS query_source VARCHAR(128),
  ADD COLUMN IF NOT EXISTS agent_id VARCHAR(64),
  ADD COLUMN IF NOT EXISTS protocol_name VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_provider VARCHAR(64),
  ADD COLUMN IF NOT EXISTS llm_model VARCHAR(128),
  ADD COLUMN IF NOT EXISTS caller_id VARCHAR(128);

CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_source_channel_timestamp
  ON agent_usage_logs(source_channel, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_protocol_name_timestamp
  ON agent_usage_logs(protocol_name, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_query_source_timestamp
  ON agent_usage_logs(query_source, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_caller_id_timestamp
  ON agent_usage_logs(caller_id, timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_surface_click_events_source_channel_created
  ON surface_click_events(source_channel, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_surface_click_events_protocol_name_created
  ON surface_click_events(protocol_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_surface_click_events_query_source_created
  ON surface_click_events(query_source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_surface_click_events_agent_id_created
  ON surface_click_events(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_source_channel_created
  ON commerce_attribution_edges(source_channel, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_protocol_name_created
  ON commerce_attribution_edges(protocol_name, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_query_source_created
  ON commerce_attribution_edges(query_source, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_attribution_edges_agent_id_created
  ON commerce_attribution_edges(agent_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_commerce_interactions_source_channel_last
  ON commerce_interactions(source_channel, last_occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_protocol_name_last
  ON commerce_interactions(protocol_name, last_occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_query_source_last
  ON commerce_interactions(query_source, last_occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_commerce_interactions_agent_id_last
  ON commerce_interactions(agent_id, last_occurred_at DESC);
