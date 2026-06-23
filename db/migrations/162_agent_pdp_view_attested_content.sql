-- 162_agent_pdp_view_attested_content.sql
-- Serve brand-attested rich content (bullet_points + usage_scenarios) on the
-- agent-read view. The E2 publish bridge (services/agent_pdp_view_assembler.py
-- assemble_row) merges these from product_enrichment; routes/agent_pdp_v1.py
-- serves them to agents alongside title/description.
-- Idempotent + additive (nullable). Prod applies via railway ssh — the
-- migration runner is dev-only.
BEGIN;
ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS bullet_points jsonb;
ALTER TABLE agent_pdp_view ADD COLUMN IF NOT EXISTS usage_scenarios jsonb;
COMMIT;
