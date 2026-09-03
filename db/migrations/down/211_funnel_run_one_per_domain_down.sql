-- Rollback for 211. Dropping the constraint restores the pre-#2020 behaviour:
-- concurrent intakes for one domain create one row each.
DROP INDEX CONCURRENTLY IF EXISTS idx_funnel_run_one_per_domain;
