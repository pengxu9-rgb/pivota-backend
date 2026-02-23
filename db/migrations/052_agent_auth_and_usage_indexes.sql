-- 052_agent_auth_and_usage_indexes.sql
--
-- Purpose:
-- 1) Speed up hash-first API key auth lookups across both schema variants:
--    - api_keys (newer path)
--    - agent_api_keys (legacy advanced schema)
-- 2) Keep legacy agents.api_key fallback path indexed.
-- 3) Speed up per-agent rate/quota windows on agent_usage_logs.

DO $$
BEGIN
  IF to_regclass('public.api_keys') IS NOT NULL THEN
    EXECUTE '
      CREATE INDEX IF NOT EXISTS idx_api_keys_active_key_hash
      ON api_keys(key_hash)
      WHERE status = ''active''
    ';
  END IF;

  IF to_regclass('public.agent_api_keys') IS NOT NULL THEN
    EXECUTE '
      CREATE INDEX IF NOT EXISTS idx_agent_api_keys_active_key_hash
      ON agent_api_keys(key_hash)
      WHERE is_active = true
    ';
  END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_agents_api_key
  ON agents(api_key);

CREATE INDEX IF NOT EXISTS idx_agent_usage_logs_agent_ts
  ON agent_usage_logs(agent_id, "timestamp" DESC);
