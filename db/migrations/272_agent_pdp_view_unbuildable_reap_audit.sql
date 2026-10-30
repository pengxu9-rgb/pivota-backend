-- 272: agent_pdp_view_unbuildable_reap_audit -- one row per unbuildable view row deleted (2026-10-30).
--
-- services/agent_pdp_view_assembler.delete_agent_pdp_view_if_unbuildable deletes the agent_pdp_view
-- row of a content_key whose catalog rows all come from a quarantined source (or all lack a title),
-- so a quarantined page stops being cited (owner decision 2026-09-29). The view is a cache, but the
-- delete is not free: agent_decision_funnel_links.content_key REFERENCES agent_pdp_view ON DELETE
-- SET NULL (migration 140), so each delete nulls attribution evidence. This table makes the delete
-- reversible: the audit INSERT and the DELETE are one statement, and the code deletes nothing while
-- the table is missing.
--
--   row_snapshot      to_jsonb() of the deleted view row, every column
--   funnel_link_ids   the link_ids whose content_key the delete nulls, read before the FK action
--   reason            all_members_quarantined | no_titled_unquarantined_member
--   run_id            refresh:<hex> for a refresh-path delete, unbuildable_sweep:<hex> for a sweep
--   restored_at       stamped by restore_reaped_agent_pdp_view_rows once the key is rebuilt (and its
--                     links re-pointed); NULL rows are the restore queue
--
-- Production deploys skip db/migrations/, so db/schema_guard.py creates the same table at boot.
-- schema-guard-exempt: CREATE TABLE only, no ADD COLUMN; schema_guard.py mirrors it.

CREATE TABLE IF NOT EXISTS agent_pdp_view_unbuildable_reap_audit (
    audit_id            BIGSERIAL PRIMARY KEY,
    content_key         TEXT NOT NULL,
    pivota_signature_id TEXT,
    reason              TEXT NOT NULL,
    run_id              TEXT NOT NULL,
    refresh_source      TEXT,
    funnel_link_count   INTEGER NOT NULL DEFAULT 0,
    funnel_link_ids     JSONB NOT NULL DEFAULT '[]'::jsonb,
    row_snapshot        JSONB NOT NULL,
    deleted_at          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    restored_at         TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_unbuildable_reap_audit_pending
    ON agent_pdp_view_unbuildable_reap_audit (content_key)
    WHERE restored_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_agent_pdp_view_unbuildable_reap_audit_run
    ON agent_pdp_view_unbuildable_reap_audit (run_id);
