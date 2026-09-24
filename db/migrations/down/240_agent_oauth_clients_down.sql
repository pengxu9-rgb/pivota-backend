-- Reverses 240_agent_oauth_clients.sql. Dropping the table un-credits every OAuth-connected client:
-- links issued afterwards are agent-less. Already-issued click rows keep their agent_id. The
-- provisioned clients themselves (mcp_oauth_clients) are untouched and keep working for OAuth.
--
-- NOT STICKY ON ITS OWN: main.py imports db.agent_oauth_clients, so metadata.create_all recreates an
-- empty table at the next startup. Remove that import (or the module) in the same release if the table
-- must stay gone.
DROP INDEX IF EXISTS uq_agent_oauth_clients_active;
DROP TABLE IF EXISTS agent_oauth_clients;
