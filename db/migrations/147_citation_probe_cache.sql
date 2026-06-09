-- Probe-result cache for the citation operator.
--
-- A grounded probe for (provider, scan_mode, query-set, product/pdp context) is
-- ~stable within a day, so caching the result:
--   1. closes the partial-failure retry COGS leak — a retried scan re-uses the
--      already-probed providers' results instead of re-hitting the LLM, and
--   2. makes same-day / recurring scans affordable (cache hit = no COGS).
--
-- Rechecks deliberately BYPASS this cache (they measure citation change over
-- time and must always probe fresh). TTL is enforced in the read query
-- (created_at > cutoff), so stale rows are simply ignored; a periodic delete
-- can reclaim them.

CREATE TABLE IF NOT EXISTS citation_probe_cache (
  cache_key    TEXT PRIMARY KEY,          -- sha256(provider|scan_mode|canonical context)
  provider     TEXT NOT NULL,
  scan_mode    TEXT NOT NULL,
  result_jsonb JSONB NOT NULL,            -- the probe result (raw_runs etc.)
  created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_citation_probe_cache_created
  ON citation_probe_cache (created_at);

-- DOWN (manual rollback only):
-- DROP TABLE IF EXISTS citation_probe_cache;
