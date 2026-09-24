-- Reverses 240_agent_oauth_clients.sql. Dropping the table un-credits every OAuth-connected
-- client: links issued afterwards are agent-less. Already-issued click rows keep their agent_id.
DROP INDEX IF EXISTS uq_agent_oauth_clients_active;
DROP TABLE IF EXISTS agent_oauth_clients;
