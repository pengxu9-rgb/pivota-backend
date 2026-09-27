-- 244: a durable per-day ledger for once-a-day scheduler jobs (db/scheduler_job_slots.py).
--
-- APScheduler's schedule is in memory, so a daily run killed by a worker redeploy was not retried
-- until the next day: nightly_index_health on 2026-09-23 and 2026-09-27. This table records, per job
-- and per UTC slot date, how many runs started and whether one finished, so the replacement instance
-- can re-run a slot that never completed (jobs/nightly_index_health_job.run_nightly_index_health_catch_up).
--
-- Migrations do not self-apply in prod; db/scheduler_job_slots.ensure_scheduler_job_slots_table()
-- runs the identical CREATE at first use. This file is the record, and what the SQL gates plan against.
-- slot_date is TEXT ('YYYY-MM-DD') so the same statements run on SQLite in tests.

CREATE TABLE IF NOT EXISTS scheduler_job_slots (
  job_id            TEXT NOT NULL,
  slot_date         TEXT NOT NULL,
  attempts          INTEGER NOT NULL DEFAULT 0,
  first_started_at  TIMESTAMPTZ,
  last_started_at   TIMESTAMPTZ,
  completed_at      TIMESTAMPTZ,
  PRIMARY KEY (job_id, slot_date)
);
