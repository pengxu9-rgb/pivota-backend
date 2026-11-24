-- Reverses 297. REVERT THE CODE FIRST: the boot heal (db/schema_guard.ensure_required_schema_light) re-adds
-- the column at the next start otherwise. With the column gone and the code still live, nothing breaks:
-- db/agents.get_buyer_display_name's read fails, the AgenPas read drops the name and the page shows no
-- "Requested by" line. Dropping the column forgets every reviewed name; re-setting them is a new one-off.
ALTER TABLE IF EXISTS agents DROP COLUMN IF EXISTS buyer_display_name;
