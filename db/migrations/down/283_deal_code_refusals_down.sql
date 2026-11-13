-- Drops deal_code_refusals (migration 283). Turn DEAL_VERIFY_JOB_ENABLED off first: the verifier refuses to
-- run without this table. Dropping forgets which codes stores refused, so the next run may test them again.
DROP TABLE IF EXISTS deal_code_refusals;
