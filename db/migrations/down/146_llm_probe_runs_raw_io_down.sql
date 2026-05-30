ALTER TABLE llm_probe_runs
  DROP COLUMN IF EXISTS request_payload_jsonb,
  DROP COLUMN IF EXISTS response_jsonb,
  DROP COLUMN IF EXISTS model;
