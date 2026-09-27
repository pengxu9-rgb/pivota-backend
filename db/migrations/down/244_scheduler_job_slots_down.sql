-- Reverses 244_scheduler_job_slots.sql. Without the table, nightly_index_health_catch_up skips every
-- tick (it fails closed on an unreadable ledger) and the 04:00 cron still runs as before.
--
-- NOT STICKY ON ITS OWN: db/scheduler_job_slots.ensure_scheduler_job_slots_table() recreates it the next
-- time the nightly job runs. Remove that call in the same release if the table must stay gone.
DROP TABLE IF EXISTS scheduler_job_slots;
