-- Reverse of 227_reap_agentic_buyer_consent.sql.
--
-- WHAT THIS DESTROYS. `consent_version` / `consented_at` are the ONLY record of the terms under
-- which each buyer's durable identity — and therefore their stored-card enrollment — was
-- established. Nothing else in this database or at Reap holds it: the purchase rows record that
-- a buyer approved a hosted page, not which version of our terms they were shown first. Dropping
-- these columns does not refuse anything and does not fail closed; it silently turns "enrolled
-- under v1" into "enrolled under nothing we can name", for every buyer at once, and the next
-- POST simply writes a fresh tag as though the earlier consent had never been recorded.
--
-- So this is NOT a safe rollback of an armed rail. Roll the ROUTE back if the consent gate is
-- the problem — the columns being present costs nothing while nothing writes them. Run this only
-- on a database where the rail has never been armed.
--
-- The columns are dropped and the table is left in place: 226 owns the table, and dropping it
-- here would strand every enrolled buyer's opaque ref (see down/226's first paragraph).
ALTER TABLE IF EXISTS reap_agentic_buyer_refs
    DROP COLUMN IF EXISTS consented_at,
    DROP COLUMN IF EXISTS consent_version;
