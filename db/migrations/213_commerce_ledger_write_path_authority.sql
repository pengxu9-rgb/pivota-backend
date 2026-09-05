-- Trust provenance on the canonical commerce ledger.
--
-- Until now the only record of WHO wrote an event was `source` and `surface`,
-- and both are strings copied from the caller's payload. The merchant HMAC
-- collector accepts any value there, so a server-side collector could write
-- `source='stripe_webhook', surface='psp'` and be indistinguishable from the
-- PSP bridge. The funnel's refund de-duplication and the ops-canary exclusion
-- both keyed on those strings.
--
-- These four columns are stamped by the ingress that authenticated the caller
-- (services/merchant_event_ingest_service.py LEDGER_AUTHORITY_BY_WRITE_PATH)
-- and never taken from the event body:
--
--   write_path                the concrete ingress, e.g. cafe24_webhook
--   authority                 observational | merchant | platform | psp
--   agent_identity_confidence the ingress trust tier for any agent claim
--   synthetic                 ops probe rows; excluded from default funnels
--
-- All nullable except `synthetic` (DEFAULT FALSE is metadata-only on PG 11+,
-- no table rewrite). Rows written before this migration keep NULL provenance,
-- which is honest: the funnel falls back to the legacy string inference only
-- for those rows.
--
-- The partial index for the retention sweep lives in 214: it is built without
-- blocking writers, and the runner executes such files on autocommit. Do not
-- write that keyword here even in prose: db/sql_migrations.py classifies a
-- file by a regex over its whole body, comments included.

ALTER TABLE commerce_interaction_events
  ADD COLUMN IF NOT EXISTS write_path VARCHAR(48) NULL,
  ADD COLUMN IF NOT EXISTS authority VARCHAR(16) NULL,
  ADD COLUMN IF NOT EXISTS agent_identity_confidence VARCHAR(24) NULL,
  ADD COLUMN IF NOT EXISTS synthetic BOOLEAN NOT NULL DEFAULT FALSE;
