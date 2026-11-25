-- 297: THE NAME A BUYER SEES FOR AN AGENT, reviewed by Pivota (AgenPas: "Requested by <name>").
--
-- agents.agent_name is whatever the agent's developer typed at registration: self-declared and never
-- reviewed, so it is never shown on a payment page (a name like "Your Bank" there is a phishing lever).
-- This column holds the only name the buyer page may show. Pivota sets it BY HAND, per agent, as a gated
-- prod one-off after checking who the agent is. NULL (every agent, until then) means the page shows no
-- "Requested by" line at all. No code path writes it.
--
-- Read by db/agents.get_buyer_display_name for the AgenPas internal purchase read
-- (routes/agent_internal_commerce_purchases), which re-checks the stored value's shape and shows nothing
-- when it fails. The column is NOT on the SQLAlchemy `agents` Table, so no existing agents SELECT names it.
--
-- Nullable, no default: on Postgres 11+ a catalog-only change, no table rewrite. Healed at every boot by
-- db/schema_guard.ensure_required_schema_light (the same ALTER, run only while the column is missing, under
-- the heal lock_timeout, so a busy agents table costs one retry next boot instead of stalling auth).

ALTER TABLE IF EXISTS agents
  ADD COLUMN IF NOT EXISTS buyer_display_name VARCHAR(64);
