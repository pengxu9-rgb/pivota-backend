-- Reverses 292. Revert the code first: with the column gone, the first PUT that mints a secret and
-- every rotation fail. Dropping it forgets which secrets were shown, so after a re-apply every
-- rotation is immediate (no 24 h overlap) until the next rotation stamps it again. The boot heal
-- (services/agent_webhook_service.ensure_agent_webhook_tables) re-adds it at the next start unless
-- the code is reverted too.
ALTER TABLE IF EXISTS agent_webhook_configs DROP COLUMN IF EXISTS secret_revealed_at;
