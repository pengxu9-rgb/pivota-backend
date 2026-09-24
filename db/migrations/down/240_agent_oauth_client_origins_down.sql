-- Reverses 240_agent_oauth_client_origins.sql. Dropping the table un-credits every OAuth-connected
-- client: links issued afterwards are agent-less. Already-issued click rows keep their agent_id.
--
-- NOT STICKY ON ITS OWN: main.py imports db.agent_oauth_client_origins, so metadata.create_all
-- recreates an empty table at the next startup. Remove that import (or the module) in the same
-- release if the table must stay gone.
DROP INDEX IF EXISTS uq_agent_oauth_client_origins_active;
DROP TABLE IF EXISTS agent_oauth_client_origins;
