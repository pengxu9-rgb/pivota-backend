-- Shopping Briefs (Brief v0.1.0) persistence
-- Migration 046: Create shopping_briefs as a durable join key across intent -> quote -> order -> outcome.

CREATE TABLE IF NOT EXISTS shopping_briefs (
  brief_id TEXT PRIMARY KEY,
  schema_version TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  vertical TEXT NOT NULL,
  market TEXT,
  locale TEXT,
  currency TEXT,
  raw_intent TEXT NOT NULL,
  brief_json JSONB NOT NULL,
  status TEXT NOT NULL DEFAULT 'active',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_shopping_briefs_agent_id ON shopping_briefs(agent_id);
CREATE INDEX IF NOT EXISTS idx_shopping_briefs_vertical ON shopping_briefs(vertical);
CREATE INDEX IF NOT EXISTS idx_shopping_briefs_status ON shopping_briefs(status);
CREATE INDEX IF NOT EXISTS idx_shopping_briefs_created_at ON shopping_briefs(created_at);

