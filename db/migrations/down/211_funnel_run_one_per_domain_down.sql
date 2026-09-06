-- Rollback for 211. Restores the pre-#2020 behaviour: concurrent intakes for
-- one domain create one row each. Non-CONCURRENT for the same reason as the
-- up-migration — the admin runner applies these inside a transaction.
DROP INDEX IF EXISTS idx_funnel_run_one_per_domain;
