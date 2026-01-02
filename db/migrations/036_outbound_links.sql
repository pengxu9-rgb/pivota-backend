-- Outbound links rules + click telemetry (platform feature for multi-vertical tools)

CREATE TABLE IF NOT EXISTS outbound_link_rules (
  id TEXT PRIMARY KEY,
  market TEXT NOT NULL,
  tool TEXT NOT NULL DEFAULT '*',
  scope TEXT NOT NULL,
  scope_id TEXT NOT NULL,
  destination_url TEXT NOT NULL,
  purchase_enabled_override BOOLEAN NULL,
  priority INTEGER NOT NULL DEFAULT 0,
  partner_type TEXT NOT NULL DEFAULT 'unknown',
  disclosure_text TEXT NULL,
  utm_template TEXT NULL,
  tags JSONB NULL,
  notes TEXT NULL,
  start_at TIMESTAMPTZ NULL,
  end_at TIMESTAMPTZ NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_by TEXT NULL,
  approved_by TEXT NULL,
  published_at TIMESTAMPTZ NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_outbound_link_rules_lookup
  ON outbound_link_rules (market, tool, status, scope, scope_id);

CREATE TABLE IF NOT EXISTS outbound_click_events (
  id BIGSERIAL PRIMARY KEY,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  market TEXT NOT NULL,
  tool TEXT NOT NULL,
  rule_id TEXT NULL,
  job_id TEXT NULL,
  session_id TEXT NULL,
  sku_id TEXT NULL,
  brand TEXT NULL,
  category TEXT NULL,
  area TEXT NULL,
  kind TEXT NULL,
  dest_domain TEXT NULL,
  destination_url TEXT NULL,
  context JSONB NULL,
  user_agent TEXT NULL,
  ip TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_outbound_click_events_market_tool_created
  ON outbound_click_events (market, tool, created_at);

