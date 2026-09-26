-- Reverse of 239_surface_click_events_issued_at.sql.
--
-- Roll the CODE back first: the current build names issued_at in issue_clicks, in the /r upsert
-- and in the surface_click_events model (every select(surface_click_events) lists it), so dropping
-- the column under it raises UndefinedColumn on every click write and every closure's click lookup.
-- What is lost: which rows were issued before they were clicked; the funnel's issued count.

ALTER TABLE IF EXISTS surface_click_events
  DROP COLUMN IF EXISTS issued_at;
