-- 278: THE A2A TOKEN RAIL WORKER'S HEARTBEAT (payment orchestration). One row, stamped at the start of
-- every armed worker tick (jobs/a2a_rail_purchase_poll). The create route runs on the web service and
-- cannot see the worker's settings; it reads this row and opens no A2A purchase unless a tick started
-- within the last 15 minutes, because a purchase opened while nothing ticks never advances, never
-- expires and never has its contact purged.
--
-- The same DDL is in db/a2a_rail_ledger (`ensure_a2a_rail_schema`), run by
-- db/schema_guard.ensure_required_schema_light on both dialects; tests/test_a2a_rail_ledger_postgres.py
-- proves the two build the same schema. A new, empty table: it locks nothing that exists.
--
-- schema-guard-exempt: this migration adds no column to an existing table.

CREATE TABLE IF NOT EXISTS a2a_rail_worker_heartbeat (
    id SMALLINT PRIMARY KEY CONSTRAINT ck_a2a_rail_worker_heartbeat_one CHECK (id = 1),
    ticked_at TIMESTAMPTZ NOT NULL
);
