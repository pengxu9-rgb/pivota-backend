ALTER TABLE llm_probe_runs
  ADD COLUMN IF NOT EXISTS request_payload_jsonb JSONB,
  ADD COLUMN IF NOT EXISTS response_jsonb JSONB,
  ADD COLUMN IF NOT EXISTS model TEXT;

COMMENT ON COLUMN llm_probe_runs.request_payload_jsonb IS
  'Full request body sent to provider (messages + params). Redact secrets before insert.';
COMMENT ON COLUMN llm_probe_runs.response_jsonb IS
  'Full response body returned by provider (choices + usage + finish_reason).';
COMMENT ON COLUMN llm_probe_runs.model IS
  'Model id actually called (after override resolution).';
