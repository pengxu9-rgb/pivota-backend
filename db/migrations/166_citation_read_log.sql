-- 166_citation_read_log.sql
-- B④-P1 (commerce-index publish arc): external attribution telemetry.
--
-- citation_observations (migration 159) records who cited a MERCHANT's
-- products inside OUR audit sampling — the inbound, audit-observed half of
-- "who cites us". This table records the OTHER half: every external read of
-- the citation surface (`GET /agent/v1/citation/{id}` and `/search`) — i.e.
-- real frontier agents pulling our CitationItem in order to cite Pivota.
-- Together they answer "who cites us" from both directions.
--
-- One row per inbound read. Best-effort, off the response hot path; a write
-- failure must never affect the read response (see db/citation_read_log.py).
-- No PII / merchant-private data: only the caller's self-declared
-- `X-Pivota-Agent` id (else client IP as a coarse identity) and what they
-- asked for. Deliberately NOT idempotent — each call is a distinct event.
--
-- Idempotent DDL. Prod skips the migration runner — apply via railway ssh /
-- admin (matches migration 159).
BEGIN;

CREATE TABLE IF NOT EXISTS citation_read_log (
    read_id       UUID PRIMARY KEY,
    endpoint      TEXT NOT NULL,            -- 'item' (single-id read) | 'search'
    requested_id  TEXT NULL,               -- raw path id on item reads (ck / sig / ext / pg)
    content_key   VARCHAR(64) NULL,        -- resolved canonical key when the read hit
    query         TEXT NULL,               -- free-text q on search reads
    intent        TEXT NULL,               -- inform | shop | ... on search reads
    status        TEXT NOT NULL,           -- hit | miss | empty | suppressed | disabled
    result_count  INTEGER NULL,            -- # citations returned on search reads
    agent         TEXT NULL,               -- self-declared X-Pivota-Agent (e.g. openai-chatgpt/1.0)
    client_ip     TEXT NULL,               -- coarse identity when no agent header
    observed_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Entity-centric read: how often is THIS product pulled for citation, over time.
CREATE INDEX IF NOT EXISTS idx_citation_read_log_content_key
  ON citation_read_log (content_key, observed_at DESC);

-- Agent-centric read: which frontier agents call the surface, how much.
CREATE INDEX IF NOT EXISTS idx_citation_read_log_agent
  ON citation_read_log (agent, observed_at DESC);

-- Recency scan (dashboards / rolling-window aggregates).
CREATE INDEX IF NOT EXISTS idx_citation_read_log_observed
  ON citation_read_log (observed_at DESC);

COMMIT;
