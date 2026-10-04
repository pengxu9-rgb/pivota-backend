-- Preserve the column during a runtime rollback. Dropping it while a 'quoting' row has a checkout
-- create in flight would remove the only bound on its replay window; the previous code never
-- reads it, so leaving it in place is inert.
SELECT 1;
