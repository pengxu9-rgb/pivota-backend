-- 292: WHEN AN AGENT WEBHOOK SIGNING SECRET WAS LAST SHOWN IN FULL.
--
-- A signing secret used to be minted silently (the legacy agents.webhook_url migration, the first
-- PUT /agents/{id}/webhooks/config, the emit path) and never shown; only a rotation showed one, and
-- that one stayed pending for 24 h while deliveries were still signed with the unseen secret. Now
-- the first PUT that mints the secret shows it once and stamps this column, and a rotation stamps it
-- too. NULL means the current secret has never been shown to anyone: a rotation then replaces it at
-- once (services/agent_webhook_service.rotate_signing_secret) instead of leaving it signing for 24 h.
-- The column holds a time only, never a secret.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite. Existing rows stay
-- NULL (their secret was never shown by the PUT, so a rotation of one is immediate).
--
-- schema-guard-exempt: the self-heal lives in services/agent_webhook_service.ensure_agent_webhook_tables
-- (the same ALTER, the last statement of _AGENT_WEBHOOK_DDL_STATEMENTS, run through
-- schema_guard.guarded_statements on Postgres: a lock-free catalog check, then the ALTER under the heal
-- lock_timeout only while the column is missing), which main.startup_event and every webhook request
-- run (a fast-mode boot skips db/migrations/). SQLite gets the column from that module's CREATE TABLE.

ALTER TABLE IF EXISTS agent_webhook_configs
  ADD COLUMN IF NOT EXISTS secret_revealed_at TIMESTAMP;
