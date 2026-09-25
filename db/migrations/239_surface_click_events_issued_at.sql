-- WHEN A CLICK ID WAS ISSUED, as opposed to when a buyer first followed it (ADR-025 D1).
--
-- Until now a surface_click_events row was written only when a buyer hit `/r`, so a link an agent
-- was handed but that nobody followed left no trace, and "links issued vs clicked" read as zero.
-- services/commerce_attribution_service.issue_clicks now writes the row when the link is ISSUED,
-- with click_count = 0, and `/r` only increments it.
--
-- There is deliberately no `state` column to keep in sync. The row existing and click_count > 0
-- already say "issued" and "clicked". issued_at is the one fact they cannot give: a row created
-- by `/r` for a link issued before this change looks like any other row. NULL issued_at = a legacy
-- row (first written at click time), so the funnel can keep the two populations apart.
--
-- Production deploys skip db/migrations/, so db/schema_guard.ensure_required_schema_light carries
-- the same column (Postgres and SQLite blocks).

ALTER TABLE IF EXISTS surface_click_events
  ADD COLUMN IF NOT EXISTS issued_at TIMESTAMPTZ;
