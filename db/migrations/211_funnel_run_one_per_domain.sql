-- #2020: one unclaimed funnel run per domain, enforced by the database.
--
-- The public intake is UNAUTHENTICATED and creates merchant_audit_runs rows.
-- Its per-domain reuse is SELECT-then-INSERT with nothing atomic behind it,
-- so N concurrent requests for one domain created N rows — measured at 50/50
-- on PG 15. The daily cap does not help: it is a plain SELECT count(*), and
-- concurrent requests all read the same value.
--
-- This index makes the reuse a real constraint. record_anonymous_funnel_run
-- catches the violation and re-reads the winner's row, so a race resolves to
-- one run instead of failing.
--
-- SAFE TO CREATE: the producer has never worked in production (the fence it
-- shipped with could not fire — see #2021), so there are no funnel rows and
-- therefore no duplicates for the unique build to trip on. Verify before
-- applying if that assumption has aged:
--   SELECT partial_result_jsonb->'funnel'->>'domain' AS d, count(*)
--     FROM merchant_audit_runs
--    WHERE merchant_id IS NULL AND subject_type = 'public_funnel'
--    GROUP BY 1 HAVING count(*) > 1;
--
-- CONCURRENTLY because merchant_audit_runs is a hot table and a plain CREATE
-- INDEX takes a lock that blocks writes to it. The runner detects the keyword
-- and switches to an AUTOCOMMIT connection (db/sql_migrations.needs_autocommit).
-- The cost of CONCURRENTLY is that a failed build leaves an INVALID index
-- behind; check with:
--   SELECT indexrelid::regclass FROM pg_index
--    WHERE NOT indisvalid AND indexrelid::regclass::text
--          = 'idx_funnel_run_one_per_domain';
-- and DROP INDEX it before retrying.
--
-- The predicate is deliberately narrow: only UNCLAIMED rows in THIS lane.
-- Once a run is claimed it belongs to a merchant, and a second visitor to the
-- same domain must be able to start a fresh one.

CREATE UNIQUE INDEX CONCURRENTLY IF NOT EXISTS idx_funnel_run_one_per_domain
  ON merchant_audit_runs ((partial_result_jsonb->'funnel'->>'domain'))
  WHERE merchant_id IS NULL AND subject_type = 'public_funnel';
