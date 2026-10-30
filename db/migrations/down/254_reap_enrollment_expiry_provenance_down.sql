-- Preserve the provenance column during runtime rollback. Dropping it would turn
-- unresolved malformed provider responses into guessed usable enrollment deadlines.
SELECT 1;
