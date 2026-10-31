-- 273: a btree on agent_decision_funnel_links.content_key (2026-10-30).
--
-- content_key REFERENCES agent_pdp_view(content_key) ON DELETE SET NULL (migration 140) and had no
-- index, so every agent_pdp_view delete ran its FK action as a sequential scan of this table, and so
-- do the unbuildable reap's link reads (services/agent_pdp_view_assembler: the delete's `links` CTE,
-- the sweep's per-key count, the restore's re-point) and the read-only census. Small today (about
-- 48 KB with its indexes on the 2026-08-22 production clone), so the build is quick; partial on
-- NOT NULL because the reads all name a key and the NULLs are exactly the rows nothing joins.
--
-- CONCURRENTLY, like 237/243/262: the runner sends a file naming that word on an AUTOCOMMIT
-- connection, so this statement stays alone in its file. db/schema_guard.py carries the plain
-- form behind the heal lock_timeout (a guard transaction cannot build CONCURRENTLY).
-- schema-guard-exempt: index only, no column added.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_agent_decision_funnel_links_content_key
  ON agent_decision_funnel_links (content_key)
  WHERE content_key IS NOT NULL;
