-- Per-provider visibility snapshots for the citation operator dashboard.
--
-- Each fresh scan writes one row per provider so the dashboard can show
-- "your AI visibility, split by assistant" and a trend over time WITHOUT
-- re-probing (viewing the dashboard must be free, not COGS). Cached scans do
-- NOT snapshot (no new information, would duplicate trend points).

CREATE TABLE IF NOT EXISTS citation_scan_runs (
  scan_run_id         TEXT PRIMARY KEY,
  merchant_id         TEXT NOT NULL,
  provider            TEXT NOT NULL,          -- chatgpt | gemini
  sku_key             TEXT,
  runs                INTEGER NOT NULL DEFAULT 0,   -- grounded probes this scan
  merchant_cited_runs INTEGER NOT NULL DEFAULT 0,   -- of which cited the merchant
  visibility_score    INTEGER,                      -- round(100 * cited / runs)
  channel_mix_jsonb   JSONB,                        -- gap-source channel histogram
  gap_count           INTEGER NOT NULL DEFAULT 0,   -- cited sources merchant is absent from
  created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_citation_scan_runs_merchant
  ON citation_scan_runs (merchant_id, provider, created_at DESC);

-- DOWN (manual rollback only):
-- DROP TABLE IF EXISTS citation_scan_runs;
