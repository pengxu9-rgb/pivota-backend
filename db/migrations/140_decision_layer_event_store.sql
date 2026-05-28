-- 140_decision_layer_event_store.sql
--
-- Durable decision-layer event store for agent commerce attribution.
-- Append-only: these rows are billing/attribution evidence, not mutable
-- product/account state. Ranking feature snapshots, citation decisions, and
-- recommendation traces are intentionally deferred.

CREATE TABLE IF NOT EXISTS agent_decision_events (
  decision_id VARCHAR(64) PRIMARY KEY,
  merchant_id VARCHAR(64) NULL,
  brand_id VARCHAR(64) NULL,
  surface TEXT NULL,
  channel TEXT NULL,
  agent_context JSONB NULL,
  attribution_model TEXT NOT NULL DEFAULT 'last_agent_decision_v1',
  attribution_window_seconds INTEGER NOT NULL DEFAULT 2592000,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_decision_candidates (
  candidate_id UUID PRIMARY KEY,
  decision_id VARCHAR(64) NOT NULL REFERENCES agent_decision_events(decision_id) ON DELETE RESTRICT,
  content_key VARCHAR(40) NULL,
  catalog_offer_id VARCHAR(255) NULL,
  rank_position INTEGER NULL,
  eligibility_flags JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_exposure_events (
  exposure_id UUID PRIMARY KEY,
  decision_id VARCHAR(64) NOT NULL REFERENCES agent_decision_events(decision_id) ON DELETE RESTRICT,
  content_key VARCHAR(40) NULL,
  catalog_offer_id VARCHAR(255) NULL,
  exposure_position INTEGER NULL,
  slot TEXT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS checkout_decisions (
  checkout_decision_id UUID PRIMARY KEY,
  decision_id VARCHAR(64) NULL REFERENCES agent_decision_events(decision_id) ON DELETE SET NULL,
  order_id VARCHAR(100) NULL,
  merchant_id VARCHAR(64) NULL,
  content_key VARCHAR(40) NULL,
  catalog_offer_id VARCHAR(255) NULL,
  purchase_route TEXT NULL,
  quote_context JSONB NULL,
  psp_context JSONB NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS agent_decision_funnel_links (
  link_id UUID PRIMARY KEY,
  decision_id VARCHAR(64) NULL REFERENCES agent_decision_events(decision_id) ON DELETE RESTRICT,
  funnel_event_id UUID NOT NULL REFERENCES funnel_events(event_id) ON DELETE RESTRICT,
  exposure_id UUID NULL REFERENCES agent_exposure_events(exposure_id) ON DELETE SET NULL,
  checkout_decision_id UUID NULL REFERENCES checkout_decisions(checkout_decision_id) ON DELETE SET NULL,
  content_key VARCHAR(40) NULL REFERENCES agent_pdp_view(content_key) ON DELETE SET NULL,
  catalog_offer_id VARCHAR(255) NULL REFERENCES catalog_offers(offer_id) ON DELETE SET NULL,
  commerce_attribution_edge_id VARCHAR(64) NULL REFERENCES commerce_attribution_edges(edge_id) ON DELETE SET NULL,
  merchant_id VARCHAR(64) NULL,
  attribution_model TEXT NOT NULL DEFAULT 'last_agent_decision_v1',
  attribution_window_seconds INTEGER NOT NULL DEFAULT 2592000,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_agent_decision_events_merchant_created
  ON agent_decision_events (merchant_id, created_at DESC)
  WHERE merchant_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_decision_candidates_decision
  ON agent_decision_candidates (decision_id, rank_position);

CREATE INDEX IF NOT EXISTS idx_agent_decision_candidates_content_key
  ON agent_decision_candidates (content_key)
  WHERE content_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_decision_candidates_catalog_offer
  ON agent_decision_candidates (catalog_offer_id)
  WHERE catalog_offer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_exposure_events_decision
  ON agent_exposure_events (decision_id, exposure_position);

CREATE INDEX IF NOT EXISTS idx_agent_exposure_events_content_key
  ON agent_exposure_events (content_key)
  WHERE content_key IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_exposure_events_catalog_offer
  ON agent_exposure_events (catalog_offer_id)
  WHERE catalog_offer_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_checkout_decisions_order
  ON checkout_decisions (order_id)
  WHERE order_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_checkout_decisions_decision
  ON checkout_decisions (decision_id)
  WHERE decision_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_decision
  ON agent_decision_funnel_links (decision_id, created_at DESC)
  WHERE decision_id IS NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_event
  ON agent_decision_funnel_links (funnel_event_id);

CREATE INDEX IF NOT EXISTS idx_agent_decision_funnel_links_merchant_month
  ON agent_decision_funnel_links (merchant_id, created_at DESC)
  WHERE merchant_id IS NOT NULL;
