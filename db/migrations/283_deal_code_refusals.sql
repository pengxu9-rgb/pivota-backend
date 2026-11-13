-- 283: deal_code_refusals, the hashes of crawled discount codes a store refused (ADR-027 Q18 retention,
-- peng 2026-11-07: "refused: row deleted after 7d, hash + reason kept 30d").
--
-- Written only by the crawled-code verifier (services/deal_code_verifier.py) when a store's own UCP door
-- rejects a code; read by it before every reference cart, so a dead code the crawl reads again is never
-- tested again inside 30 days. Deleted after 30 days by services/deal_code_retention.py. A row holds a
-- sha256 of (seller, market, code) and the store's reason code: never the code, never the seller.
--
-- References no other table. Also declared in db/deal_lane.py; create_all creates it at startup (prod
-- does not apply numbered migrations). This file is the record, and what the SQL gates plan against.
-- schema-guard-exempt: CREATE TABLE only, no ADD COLUMN; create_all builds the same table at boot.

CREATE TABLE IF NOT EXISTS deal_code_refusals (
  refusal_hash  VARCHAR(64) PRIMARY KEY,
  reason        VARCHAR(128),
  refused_at    TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_deal_code_refusals_refused_at
  ON deal_code_refusals (refused_at);
